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
        self.processed_messages = set()  # 用于去重的消息ID集合
        self.message_cache = {}  # 缓存最近的消息，用于引用查找

    def on_message(self, ws, message):
        '''处理收到的消息'''
        try:
            msg_data = json.loads(message)

            # 过滤掉 API 响应消息
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

            # 缓存消息内容供引用查找使用
            if message_id and raw_message:
                # 统一使用字符串类型作为key
                self.message_cache[str(message_id)] = raw_message
                # 限制缓存大小，避免内存泄漏
                if len(self.message_cache) > 100:
                    # 删除最旧的50条
                    old_keys = list(self.message_cache.keys())[:50]
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
                self.send_reply(msg_data, '🍥 Nino Bot Help\n#nino help - 获取帮助\n#nino <消息> - 与nino对话\n#nino pass <密钥> - 设置隔离密钥\n#nino dashboard - 获取面板地址')
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

            # 处理普通对话
            if content:
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
                                # 尝试获取引用消息的内容
                                reply_msg = self.get_quoted_message(reply_id)
                                if reply_msg:
                                    reply_info = f"[回复: \"{reply_msg}\"]\n"

                        # 处理图片
                        elif seg.get('type') == 'image':
                            img_url = seg.get('data', {}).get('url', '')
                            if img_url:
                                image_desc = core.process_image(img_url, user_id)
                                if image_desc:
                                    image_desc = f"[图片:\"{image_desc}\"]"

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
                    self.send_reply(msg_data, f'[自动回复] 咱现在不在哦w...\nDebug: {str(e)}')

        except Exception as e:
            print(f'Error processing message: {e}')

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

        except Exception as e:
            print(f'Error sending reply: {e}')

    def get_quoted_message(self, message_id):
        '''从缓存中获取引用消息的内容'''
        try:
            # 先从缓存中查找
            if message_id in self.message_cache:
                cached_msg = self.message_cache[message_id]
                # 清理CQ码，只返回纯文本
                clean_msg = re.sub(r'\[CQ:[^\]]*\]', '', cached_msg).strip()
                # 限制长度，避免引用内容过长
                if len(clean_msg) > 100:
                    clean_msg = clean_msg[:100] + '...'
                return clean_msg
            else:
                # 如果缓存中没有，返回一个通用提示
                return "获取引用消息失败"

        except Exception as e:
            print(f'Error getting quoted message: {e}')
            return "获取引用消息失败"

    def on_error(self, ws, error):
        '''处理错误'''
        print(f'WebSocket Error: {error}')
        if self.should_reconnect:
            print(f'将在 {self.reconnect_interval} 秒后尝试重连...')

    def on_close(self, ws, close_status_code, close_msg):
        '''连接关闭'''
        print(f'WebSocket connection closed: {close_status_code} - {close_msg}')
        self.running = False
        if self.should_reconnect:
            print(f'将在 {self.reconnect_interval} 秒后尝试重连...')
            threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def on_open(self, ws):
        '''连接建立'''
        print('WebSocket connected')
        self.running = True

    def _reconnect_loop(self):
        '''重连循环，每30秒尝试重连一次'''
        while self.should_reconnect and not self.running:
            time.sleep(self.reconnect_interval)
            if not self.running and self.should_reconnect:
                print('尝试重新连接 WebSocket...')
                try:
                    self.connect()
                except Exception as e:
                    print(f'重连失败: {e}')

    def connect(self):
        '''建立WebSocket连接'''
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

        # 在单独的线程中运行
        ws_thread = threading.Thread(target=self._run_with_reconnect, daemon=True)
        ws_thread.start()

    def _run_with_reconnect(self):
        '''运行 WebSocket 连接，失败时触发重连'''
        try:
            self.ws.run_forever()
        except Exception as e:
            print(f'WebSocket 运行错误: {e}')
            if self.should_reconnect and not self.running:
                print(f'将在 {self.reconnect_interval} 秒后尝试重连...')
                threading.Thread(target=self._reconnect_loop, daemon=True).start()

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
