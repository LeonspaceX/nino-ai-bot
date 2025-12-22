import json
import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()


def _json_dump(context, file_path):
    json.dump(
        context,
        open(file_path, mode='w', encoding='UTF-8'),
        ensure_ascii = False,
        indent       = 4
    )


def _user_paths(user_id: str | None = None) -> dict:
    '''
    获取用户数据文件路径。

    :param user_id: 用户ID，如果为None则返回默认路径
    '''
    if user_id is None:
        return {
            'context': 'data/context.json',
            'memory':  'data/memory.json'
        }
    base = f'data/{user_id}'
    if not os.path.exists(base):
        os.makedirs(base, exist_ok=True)
    ctx = f'{base}/context.json'
    mem = f'{base}/memory.json'
    if not os.path.exists(ctx):
        _json_dump([], ctx)
    if not os.path.exists(mem):
        _json_dump([], mem)
    return {
        'context': ctx,
        'memory':  mem
    }


def load_data(user_id: str | None = None) -> dict[str]:
    '''
    从数据库和环境变量加载数据。

    :param user_id: 用户ID，如果为None则使用默认数据
    '''
    paths = _user_paths(user_id)
    config = json.load(open('data/config.json', encoding='UTF-8'))

    # 从环境变量读取敏感配置（优先使用环境变量）
    config['ai_api_key'] = os.getenv('AI_API_KEY', config.get('ai_api_key', ''))
    config['visual_api_key'] = os.getenv('VISUAL_API_KEY', config.get('visual_api_key', ''))
    config['onebot_token'] = os.getenv('ONEBOT_TOKEN', config.get('onebot_token', ''))

    return {
        'context': json.load(open(paths['context'], encoding='UTF-8')),
        'memory':  json.load(open(paths['memory'], encoding='UTF-8')),
        'config':  config
    }


def add_data(mode: str, new_data: str, user_id: str | None = None) -> None:
    '''
    添加新数据到数据库。

    :param mode: 添加到哪个数据库？（取值`'context'`、`'memory'`）
    :param new_data: 要添加的数据。
    :param user_id: 用户ID

    注意：修改config数据库请使用`update_config()`
    '''
    paths = _user_paths(user_id)
    if mode == 'context':
        context_list = json.load(open(paths['context'], encoding='UTF-8'))
        if len(context_list) == 30:
            del context_list[0]
        context_list.append(new_data)
        _json_dump(context_list, paths['context'])
    elif mode == 'memory':
        memory_list = json.load(open(paths['memory'], encoding='UTF-8'))
        memory_list.append(new_data.replace('\n', ''))
        _json_dump(memory_list, paths['memory'])
    else:
        raise ValueError('Can only accept the string "context" and "memory"')


def remove_data(mode: str, target: str | None = None, user_id: str | None = None) -> None:
    '''
    从数据库删除数据。

    :param mode: 删除哪个数据库里的数据？（取值`'context'`、`'memory'`）\n
    :param target: 需要删除的数据的完整字符串。（当mode为`'context'`时无需传入，**因为会删除所有上下文数据**。）
    :param user_id: 用户ID

    注意：修改config数据库请使用`update_config()`
    '''
    paths = _user_paths(user_id)
    if mode == 'context':
        context_list = []
        _json_dump(context_list, paths['context'])
    elif mode == 'memory':
        memory_list = json.load(open(paths['memory'], encoding='UTF-8'))
        memory_list.remove(target)
        _json_dump(memory_list, paths['memory'])
    else:
        raise ValueError('Can only accept the string "context" and "memory"')


def update_config(key: str, value: str) -> None:
    '''
    修改config数据库里的数据。

    :param key: 需要修改的键。
    :param value: 需要修改的值。
    '''
    config = load_data()['config']
    if key not in config:
        raise KeyError('Key not found')
    config[key] = value
    _json_dump(config, 'data/config.json')


def get_user_token(user_id: str) -> str | None:
    '''
    获取用户的WebUI访问token。

    :param user_id: 用户ID
    '''
    try:
        tokens = json.load(open('data/pass.json', encoding='UTF-8'))
        return tokens.get(user_id)
    except Exception:
        return None


def set_user_token(user_id: str, token: str) -> None:
    '''
    设置用户的WebUI访问token。

    :param user_id: 用户ID
    :param token: token字符串
    '''
    try:
        tokens = json.load(open('data/pass.json', encoding='UTF-8'))
    except Exception:
        tokens = {}
    tokens[user_id] = token
    _json_dump(tokens, 'data/pass.json')


def verify_user_token(user_id: str, token: str) -> bool:
    '''
    验证用户token是否正确。

    :param user_id: 用户ID
    :param token: token字符串
    '''
    saved_token = get_user_token(user_id)
    return saved_token is not None and saved_token == token


def get_blacklist() -> list:
    '''
    获取黑名单列表。

    :return: 黑名单用户ID列表
    '''
    try:
        blacklist = json.load(open('data/blacklist.json', encoding='UTF-8'))
        return blacklist if isinstance(blacklist, list) else []
    except Exception:
        return []


def add_to_blacklist(user_id: str) -> bool:
    '''
    将用户添加到黑名单。

    :param user_id: 用户ID
    :return: 是否添加成功（如果已存在返回False）
    '''
    try:
        blacklist = get_blacklist()
        if user_id in blacklist:
            return False
        blacklist.append(user_id)
        _json_dump(blacklist, 'data/blacklist.json')
        return True
    except Exception:
        return False


def remove_from_blacklist(user_id: str) -> bool:
    '''
    将用户从黑名单移除。

    :param user_id: 用户ID
    :return: 是否移除成功（如果不存在返回False）
    '''
    try:
        blacklist = get_blacklist()
        if user_id not in blacklist:
            return False
        blacklist.remove(user_id)
        _json_dump(blacklist, 'data/blacklist.json')
        return True
    except Exception:
        return False


def is_blacklisted(user_id: str) -> bool:
    '''
    检查用户是否在黑名单中。

    :param user_id: 用户ID
    :return: 是否在黑名单中
    '''
    return user_id in get_blacklist()