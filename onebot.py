import websocket
import json
import threading
import time
import core
import data
import re


class OneBotClient:
    def __init__(self):
        self.config = data.load_data()['config']
        self.ws_url = self.config['onebot_ws_url']
        self.token = self.config['onebot_token']
        self.ws = None
        self.running = False
        self.should_reconnect = self.config.get('onebot_should_reconnect', True)  # 从配置读取，默认为 True
        self.reconnect_interval = self.config.get('onebot_reconnect_interval', 30)  # 从配置读取，默认为 30 秒
        self.max_reconnect_interval = self.config.get('onebot_max_reconnect_interval', 300)  # 从配置读取，默认为 300 秒（5分钟）
        self.processed_messages = set()  # 用于去重的消息ID集合

        # 异步 API 调用机制
        self.pending_api_calls = {}  # {echo: {'event': Event(), 'result': None}}
        self.api_call_lock = threading.Lock()

        # 降级方案：缓存最近的消息（用于 API 超时时的降级）
        self.message_cache = {}  # {message_id: {'text': str, 'images': [urls]}}

        # 重连控制
        self.reconnecting = False  # 防止多个重连线程同时运行
        self.reconnect_lock = threading.Lock()
        self.current_reconnect_delay = self.reconnect_interval  # 当前重连延迟（支持指数退避）

    def on_message(self, ws, message):
        '''处理收到的消息'''
        try:
            msg_data = json.loads(message)

            # 处理 API 响应（有 echo 字段且不是事件消息）
            echo = msg_data.get('echo')
            if echo and 'post_type' not in msg_data:
                self._handle_api_response(echo, msg_data)
                return

            # 如果没有 echo 但有 status 字段（某些 OneBot 实现不返回 echo）
            if not echo and 'status' in msg_data and 'post_type' not in msg_data:
                # 按 FIFO 顺序分配给最早的等待请求
                with self.api_call_lock:
                    if self.pending_api_calls:
                        # 获取最早的请求
                        earliest_echo = next(iter(self.pending_api_calls))
                        self._handle_api_response(earliest_echo, msg_data)
                        return

            # 过滤掉非事件消息
            post_type = msg_data.get('post_type')
            if not post_type or post_type not in ['message', 'notice', 'request', 'meta_event']:
                return

            # 只处理message类型的消息
            if post_type != 'message':
                return

            # 过滤掉 message_sent 类型（机器人自己发送的消息）
            if msg_data.get('message_type') == 'message_sent':
                return

            # 过滤掉 echo 事件
            if 'echo' in msg_data:
                return

            # 过滤掉机器人自己的消息
            self_id = msg_data.get('self_id')
            user_id = msg_data.get('user_id')
            if self_id and user_id and str(self_id) == str(user_id):
                return

            # 获取消息内容和用户信息
            raw_message = msg_data.get('raw_message', '')
            user_id = str(user_id or '')
            message_type = msg_data.get('message_type', '')
            message_id = msg_data.get('message_id')

            # 缓存消息内容（用于 API 超时时的降级方案）
            if message_id:
                message_chain = msg_data.get('message', [])
                if isinstance(message_chain, list):
                    text_parts = []
                    img_urls = []
                    for seg in message_chain:
                        if seg.get('type') == 'text':
                            text_parts.append(seg.get('data', {}).get('text', ''))
                        elif seg.get('type') == 'image':
                            img_url = seg.get('data', {}).get('url', '')
                            if img_url:
                                img_urls.append(img_url)

                    self.message_cache[str(message_id)] = {
                        'text': ' '.join(text_parts).strip(),
                        'images': img_urls
                    }

                    # 限制缓存大小
                    if len(self.message_cache) > 200:
                        old_keys = list(self.message_cache.keys())[:100]
                        for key in old_keys:
                            self.message_cache.pop(key, None)

            # 检查消息是否已处理（去重）
            if message_id and message_id in self.processed_messages:
                return

            # 移除消息开头的引用标签（[CQ:reply,id=xxxxx]等），以便正确识别 #nino 指令
            # 这样用户可以在引用消息时使用指令
            # 使用 (?:...)+ 匹配一个或多个连续的CQ码
            clean_message = re.sub(r'^(?:\[CQ:[^\]]*\]\s*)+', '', raw_message).strip()

            # 检查是否是 #nino 开头的消息
            if not clean_message.startswith('#nino'):
                return

            # 记录收到的消息
            print(f'[收到消息] 用户 {user_id}: {clean_message[:50]}{"..." if len(clean_message) > 50 else ""}')

            # 检查用户是否在黑名单中
            if data.is_blacklisted(user_id):
                print(f'[已忽略] 黑名单用户: {user_id}')
                return

            # 标记消息为已处理
            if message_id:
                self.processed_messages.add(message_id)
                # 限制集合大小，避免内存泄漏
                if len(self.processed_messages) > 1000:
                    self.processed_messages.clear()

            # 解析指令（使用清理后的消息）
            content = clean_message[5:].strip()  # 去掉 #nino 前缀

            # 处理help指令
            if content == 'help':
                help_msg = '🍥 Nino Bot Help\n#nino help - 获取帮助\n#nino <消息> - 与nino对话\n#nino pass <密钥> - 设置隔离密钥\n#nino dashboard - 获取面板地址'
                if self.is_owner(user_id):
                    help_msg += '\n\n👑 主人专用指令：\n#nino ban <QQ号> - 拉黑用户\n#nino unban <QQ号> - 解除拉黑'
                self.send_reply(msg_data, help_msg)
                return

            # 处理pass指令
            if content.startswith('pass '):
                token = content[5:].strip()
                if token:
                    data.set_user_token(user_id, token)
                    self.send_reply(msg_data, 'ok，密钥已设置！')
                else:
                    self.send_reply(msg_data, '请提供密钥哦～')
                return

            # 处理dashboard指令
            if content == 'dashboard':
                user_token = data.get_user_token(user_id)
                if not user_token:
                    self.send_reply(msg_data, '请先通过私聊设置密钥哦：#nino pass <密钥>')
                    return
                web_url = self.config.get('web_url', 'http://127.0.0.1:5000')
                dashboard_url = f'{web_url}/data?user={user_id}'
                self.send_reply(msg_data, f'你的面板地址：\n{dashboard_url}')
                return

            # 处理ban指令（仅主人可用）
            if content.startswith('ban '):
                if not self.is_owner(user_id):
                    self.send_reply(msg_data, '⛔ 此指令仅主人可用')
                    return
                target_id = content[4:].strip()
                if not target_id:
                    self.send_reply(msg_data, '请提供要拉黑的QQ号')
                    return
                if target_id in self.config.get('owner_ids', []):
                    self.send_reply(msg_data, '❌ 不能拉黑主人')
                    return
                if data.add_to_blacklist(target_id):
                    self.send_reply(msg_data, f'✅ 已将用户 {target_id} 加入黑名单')
                else:
                    self.send_reply(msg_data, f'ℹ️ 用户 {target_id} 已在黑名单中')
                return

            # 处理unban指令（仅主人可用）
            if content.startswith('unban '):
                if not self.is_owner(user_id):
                    self.send_reply(msg_data, '⛔ 此指令仅主人可用')
                    return
                target_id = content[6:].strip()
                if not target_id:
                    self.send_reply(msg_data, '请提供要解除拉黑的QQ号')
                    return
                if data.remove_from_blacklist(target_id):
                    self.send_reply(msg_data, f'✅ 已将用户 {target_id} 移出黑名单')
                else:
                    self.send_reply(msg_data, f'ℹ️ 用户 {target_id} 不在黑名单中')
                return

            # 处理普通对话
            if content:
                # 在单独线程中处理对话，避免阻塞 WebSocket 消息接收
                threading.Thread(
                    target=self._handle_conversation,
                    args=(msg_data, content, user_id),
                    daemon=True
                ).start()

        except Exception as e:
            print(f'[错误] 消息处理异常: {e}')

    def _handle_conversation(self, msg_data, content, user_id):
        '''处理对话消息（在单独线程中执行）'''
        try:
                # 提取引用消息和图片
                image_desc = ""
                reply_info = ""
                message_chain = msg_data.get('message', [])

                if isinstance(message_chain, list):
                    for seg in message_chain:
                        # 处理引用消息
                        if seg.get('type') == 'reply':
                            reply_id = seg.get('data', {}).get('id')
                            if reply_id:
                                # 统一转换为字符串类型
                                reply_id = str(reply_id)
                                # 获取引用消息的内容和图片
                                reply_text, reply_images = self.get_quoted_message(reply_id)

                                # 构建引用信息
                                if reply_text:
                                    reply_info = f"[回复: \"{reply_text}\"]\n"

                                # 处理引用消息中的图片（识别所有图片）
                                if reply_images:
                                    for img_url in reply_images:
                                        img_desc = core.process_image(img_url, user_id)
                                        if img_desc:
                                            reply_info += f"[图片:\"{img_desc}\"]\n"

                        # 处理当前消息中的图片（支持多张图片）
                        elif seg.get('type') == 'image':
                            img_url = seg.get('data', {}).get('url', '')
                            if img_url:
                                img_desc = core.process_image(img_url, user_id)
                                if img_desc:
                                    image_desc += f"[图片:\"{img_desc}\"]"

                # 清理CQ码标签（移除所有[CQ:...]格式的内容）
                content = re.sub(r'\[CQ:[^\]]*\]', '', content).strip()

                # 组合最终内容：引用 + 图片 + 消息内容
                final_content = reply_info + image_desc + content

                # 调用AI
                try:
                    model = self.config.get('model', 'deepseek-chat')
                    result = core.send(
                        user_input=final_content,
                        model=model,
                        memory=True,
                        double_output=True,
                        user_id=user_id,
                        image_desc=image_desc
                    )

                    # 发送主回复
                    if result.get('output'):
                        self.send_reply(msg_data, result['output'])

                    # 发送第二个气泡
                    if result.get('double_output'):
                        time.sleep(0.5)  # 短暂延迟
                        self.send_reply(msg_data, result['double_output'])

                except Exception as e:
                    self.send_reply(msg_data, f'[自动回复] 咱现在不在哦w...')
                    print(f'[错误] AI 调用失败: {e}')

        except Exception as e:
            print(f'[错误] 处理对话失败: {e}')

    def is_owner(self, user_id: str) -> bool:
        '''检查用户是否为主人'''
        owner_ids = self.config.get('owner_ids', [])
        return user_id in owner_ids

    def _handle_api_response(self, echo, response_data):
        '''处理 API 响应，唤醒等待的线程'''
        with self.api_call_lock:
            if echo in self.pending_api_calls:
                call_info = self.pending_api_calls[echo]
                call_info['result'] = response_data
                call_info['event'].set()  # 唤醒等待的线程

    def _call_api_sync(self, action, params, timeout=5):
        '''同步调用 OneBot API，返回响应结果（默认5秒超时）'''
        try:
            # 生成唯一的 echo 标识
            echo = f'{action}_{int(time.time() * 1000)}_{id(threading.current_thread())}'

            # 创建等待事件
            event = threading.Event()
            with self.api_call_lock:
                self.pending_api_calls[echo] = {
                    'event': event,
                    'result': None
                }

            # 发送 API 请求
            api_call = {
                'action': action,
                'params': params,
                'echo': echo
            }

            self.ws.send(json.dumps(api_call))

            # 等待响应（带超时）
            if event.wait(timeout):
                with self.api_call_lock:
                    call_info = self.pending_api_calls.pop(echo, None)
                    if call_info:
                        return call_info['result']
            else:
                # 超时，清理
                with self.api_call_lock:
                    self.pending_api_calls.pop(echo, None)
                return None

        except Exception as e:
            print(f'[错误] API 调用失败 {action}: {e}')
            return None

    def send_reply(self, original_msg, reply_text):
        '''发送回复消息'''
        try:
            message_type = original_msg.get('message_type')
            user_id = original_msg.get('user_id')
            group_id = original_msg.get('group_id')

            action = 'send_private_msg' if message_type == 'private' else 'send_group_msg'

            params = {
                'message': reply_text
            }

            if message_type == 'private':
                params['user_id'] = user_id
            else:
                params['group_id'] = group_id

            api_call = {
                'action': action,
                'params': params
            }

            self.ws.send(json.dumps(api_call))

            # 记录发送的回复
            preview = reply_text[:30] + '...' if len(reply_text) > 30 else reply_text
            print(f'[发送回复] 给用户 {user_id}: {preview}')

        except Exception as e:
            print(f'[错误] 发送回复失败: {e}')

    def send_private_message(self, user_id, message_text):
        '''直接发送私聊消息给指定用户'''
        try:
            api_call = {
                'action': 'send_private_msg',
                'params': {
                    'user_id': int(user_id),
                    'message': message_text
                }
            }

            self.ws.send(json.dumps(api_call))

        except Exception as e:
            print(f'[错误] 发送私聊消息失败: {e}')

    def get_quoted_message(self, message_id):
        '''通过 get_msg API 获取引用消息的完整内容，返回 (文本, [图片URL列表])'''
        try:
            # 调用 get_msg API（使用7秒超时）
            response = self._call_api_sync('get_msg', {'message_id': int(message_id)}, timeout=7)

            if not response:
                # API 超时，尝试从缓存获取
                cached = self.message_cache.get(str(message_id))
                if cached:
                    text = cached.get('text', '')
                    images = cached.get('images', [])
                    if text or images:
                        # 限制文本长度
                        if len(text) > 100:
                            text = text[:100] + '...'
                        return text, images

                return "获取引用消息失败", []

            if response.get('status') != 'ok':
                return "获取引用消息失败", []

            # 提取消息数据
            msg_data = response.get('data', {})
            message_chain = msg_data.get('message', [])

            if not isinstance(message_chain, list):
                return "获取引用消息失败", []

            # 提取文本和图片
            text_parts = []
            image_urls = []

            for seg in message_chain:
                seg_type = seg.get('type')
                seg_data = seg.get('data', {})

                if seg_type == 'text':
                    text = seg_data.get('text', '').strip()
                    if text:
                        text_parts.append(text)

                elif seg_type == 'image':
                    img_url = seg_data.get('url', '')
                    if img_url:
                        image_urls.append(img_url)

            # 合并文本
            full_text = ' '.join(text_parts)

            # 限制文本长度
            if len(full_text) > 100:
                full_text = full_text[:100] + '...'

            # 如果没有提取到任何内容
            if not full_text and not image_urls:
                return "获取引用消息失败", []

            return full_text if full_text else "", image_urls

        except Exception as e:
            print(f'[错误] 获取引用消息失败: {e}')
            return "获取引用消息失败", []

    def on_error(self, ws, error):
        '''处理错误'''
        print(f'WebSocket Error: {error}')

    def on_close(self, ws, close_status_code, close_msg):
        '''连接关闭'''
        print(f'WebSocket connection closed: {close_status_code} - {close_msg}')
        self.running = False
        if self.should_reconnect:
            self._trigger_reconnect()

    def on_open(self, ws):
        '''连接建立'''
        print('WebSocket connected')
        self.running = True

        # 重置重连延迟（连接成功后）
        self.current_reconnect_delay = self.reconnect_interval

        # 向所有主人发送启动消息
        owner_ids = self.config.get('owner_ids', [])
        if owner_ids and isinstance(owner_ids, list):
            startup_message = '🍥 Nino Bot正在运行！\n#nino <消息> 开始聊天\n#nino help 获取更多帮助'
            for owner_id in owner_ids:
                if owner_id:  # 确保不是空字符串
                    self.send_private_message(owner_id, startup_message)

    def _trigger_reconnect(self):
        '''触发重连（防止多个重连线程同时运行）'''
        with self.reconnect_lock:
            if self.reconnecting:
                # 已经有重连线程在运行，不创建新的
                return
            self.reconnecting = True

        # 在新线程中启动重连循环
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self):
        '''重连循环，使用指数退避策略'''
        try:
            while self.should_reconnect and not self.running:
                # 等待当前延迟时间
                print(f'将在 {self.current_reconnect_delay} 秒后尝试重连...')
                time.sleep(self.current_reconnect_delay)

                if not self.running and self.should_reconnect:
                    print(f'尝试重新连接 WebSocket... (当前延迟: {self.current_reconnect_delay}秒)')
                    try:
                        # 直接重建连接，不调用 connect()
                        self._start_websocket()

                        # 增加重连延迟（指数退避，但不超过最大值）
                        self.current_reconnect_delay = min(
                            self.current_reconnect_delay * 2,
                            self.max_reconnect_interval
                        )
                    except Exception as e:
                        print(f'重连失败: {e}')
        finally:
            # 重连循环结束，释放锁
            with self.reconnect_lock:
                self.reconnecting = False

    def _start_websocket(self):
        '''启动 WebSocket 连接（内部方法）'''
        # 如果已有连接，先关闭
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
            self.ws = None

        headers = []
        if self.token:
            headers.append(f'Authorization: Bearer {self.token}')

        self.ws = websocket.WebSocketApp(
            self.ws_url,
            header=headers,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )

        # 在单独的线程中运行（不触发重连）
        ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        ws_thread.start()

    def connect(self):
        '''建立WebSocket连接'''
        # 重置重连状态
        with self.reconnect_lock:
            self.reconnecting = False
        self.current_reconnect_delay = self.reconnect_interval

        # 启动 WebSocket
        self._start_websocket()

    def disconnect(self):
        '''断开连接'''
        self.should_reconnect = False  # 停止重连尝试
        if self.ws:
            self.ws.close()
            self.running = False


# 全局实例
_client = None
_client_lock = threading.Lock()


def start_onebot_client():
    '''启动OneBot客户端'''
    global _client

    with _client_lock:
        if _client is not None:
            return

        _client = OneBotClient()
        _client.connect()
        print('OneBot client started')


def stop_onebot_client():
    '''停止OneBot客户端'''
    global _client

    with _client_lock:
        if _client:
            _client.disconnect()
            _client = None
            print('OneBot client stopped')


def get_client():
    '''获取客户端实例（用于调试）'''
    return _client


if __name__ == '__main__':
    # 测试运行
    start_onebot_client()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_onebot_client()
