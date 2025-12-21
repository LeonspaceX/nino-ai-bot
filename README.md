![](readme_src/logo.png)

# Nino-Bot🍥

Nino是一款轻量级、开源的AI聊天机器人，基于[nino](https://github.com/Pinpe/nino-ai-chat)项目二次开发，专注于陪伴与理解用户。它能记住不同用户的偏好、习惯和重要信息，结合时间，用可爱温柔的语气与用户互动，可用于日常群内聊天、心理调适等场景。

## 🌟 功能特点
- **双向奔赴的陪伴**：用口语化、可爱调皮的语气交流，支持简单颜文字和软萌后缀（w、喵、捏等）
- **长期记忆能力**：自动记录你的个人信息、偏好和约定，也可手动管理记忆库
- **个性化回复**：结合当前时间、聊天上下文和附带图片生成专属回应
- **本地数据存储**：所有聊天记录、记忆、配置均保存在本地，仅必要时调用第三方API
- **响应式webui**：适配桌面端和移动端，随时管理数据
- **开源免费**：基于GPL-3.0协议，可自由修改和二次开发

## 🚀 快速开始

### 1. 环境要求
- Python 3.8+

### 2. 安装依赖
```bash
pip install -r requirements.txt
```
### 3. 配置onebot&api key
复制一份config-example.json，改名为config.json，按要求配置。

### 4. 启动程序
运行`shell.py`启动webui&onebot客户端：
```bash
python shell.py
```
服务启动后，即可通过QQ机器人聊天！

## 🛠️ 技术栈
- 后端：Python、Flask
- 前端：HTML、CSS、jQuery
- 第三方依赖：flask、openai、requests、websocket
- API服务：OPENAI API
- 数据存储：JSON文件



## ⚠️ 注意事项
1. AI回复可能存在「幻觉」（虚构信息），请理性判断，Nino及其作者不承担相关责任
2. API密钥需妥善保管，切勿泄露给他人

## 📜 开源协议
本项目基于 **GPL-3.0 开源协议** 发布，你可以自由使用、修改和分发，但必须保留原作者版权信息，且衍生作品需采用相同协议。

原作者：[Pinpe](https://github.com/Pinpe)

---

💖 希望Nino-Bot能给你带来温暖和快乐～