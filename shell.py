from flask import *
import data
import time
import onebot


shell = Flask(__name__)


@shell.route('/')
def index():
    '''首页 - 显示运行状态'''
    theme_color = data.load_data()['config'].get('theme_color', 'FF9800')
    return render_template('index.html', theme_color=theme_color)


def is_auth(user, token):
    '''验证用户认证'''
    if not user or not token:
        return False
    return data.verify_user_token(user, token)


def alert(text, redirect):
    '''弹出提示并跳转'''
    return f'''
        <script>
            alert('{text}')
            window.location.href = '{redirect}'
        </script>
    '''


@shell.route('/login')
def pub_login():
    '''登录页面'''
    user_id = request.args.get('user', '')
    error = request.args.get('error', '')
    theme_color = data.load_data()['config'].get('theme_color', 'FF9800')
    return render_template('login.html', user_id=user_id, error=error, theme_color=theme_color)


@shell.route('/login_submit', methods=['POST'])
def login_submit():
    '''处理登录'''
    user_id = request.form.get('user')
    token = request.form.get('token')

    if not is_auth(user_id, token):
        return redirect(f'/login?user={user_id}&error=认证失败')

    # 设置 cookie
    resp = redirect(f'/data?user={user_id}')
    resp.set_cookie(f'nino_token_{user_id}', token, max_age=2592000)  # 30天
    return resp


@shell.route('/data')
def pub_data():
    '''数据管理面板'''
    user = request.args.get('user')
    token = request.args.get('token') or request.cookies.get(f'nino_token_{user}')

    if not is_auth(user, token):
        return redirect(f'/login?user={user}')

    mem = data.load_data(user)['memory']
    resp = make_response(render_template(
        'data.html',
        memory_list=mem,
        tip='当前没有长期记忆，去创造美好的回忆吧qwq' if mem == [] else '',
        user_id=user
    ))

    # 如果通过URL传入token，设置cookie
    if request.args.get('token'):
        resp.set_cookie(f'nino_token_{user}', token, max_age=2592000)

    return resp


@shell.route('/add-memory', methods=['POST'])
def add_memory():
    '''添加记忆'''
    user = request.form.get('user')
    token = request.form.get('token') or request.cookies.get(f'nino_token_{user}')

    if not is_auth(user, token):
        return alert('请先登录', f'/data?user={user}')

    data.add_data('memory', request.form.get('memory_content'), user_id=user)
    return redirect(f'/data?user={user}')


@shell.route('/remove-memory', methods=['POST'])
def remove_memory():
    '''删除记忆'''
    user = request.form.get('user')
    token = request.form.get('token') or request.cookies.get(f'nino_token_{user}')

    if not is_auth(user, token):
        return alert('请先登录', f'/data?user={user}')

    data.remove_data('memory', request.form.get('memory'), user_id=user)
    return redirect(f'/data?user={user}')


@shell.route('/remove-context')
def remove_context():
    '''清空上下文'''
    user = request.args.get('user')
    token = request.args.get('token') or request.cookies.get(f'nino_token_{user}')

    if not is_auth(user, token):
        return alert('请先登录', f'/data?user={user}')

    data.remove_data('context', user_id=user)
    return redirect(f'/data?user={user}')


@shell.route('/context')
def view_context():
    '''查看上下文'''
    user = request.args.get('user')
    token = request.args.get('token') or request.cookies.get(f'nino_token_{user}')

    if not is_auth(user, token):
        return redirect(f'/login?user={user}')

    context_list = data.load_data(user)['context']
    theme_color = data.load_data()['config'].get('theme_color', 'FF9800')

    return render_template(
        'context.html',
        context_list=context_list,
        user_id=user,
        theme_color=theme_color
    )


@shell.route('/export-memory')
def export_memory():
    '''导出记忆'''
    user = request.args.get('user')
    token = request.args.get('token') or request.cookies.get(f'nino_token_{user}')

    if not is_auth(user, token):
        return alert('请先登录', f'/data?user={user}')

    paths = data._user_paths(user)
    return send_file(paths['memory'], as_attachment=True)


@shell.route('/export-context')
def export_context():
    '''导出上下文'''
    user = request.args.get('user')
    token = request.args.get('token') or request.cookies.get(f'nino_token_{user}')

    if not is_auth(user, token):
        return alert('请先登录', f'/data?user={user}')

    paths = data._user_paths(user)
    return send_file(paths['context'], as_attachment=True)


@shell.route('/import-memory', methods=['POST'])
def import_memory():
    '''导入记忆'''
    user = request.form.get('user')
    token = request.form.get('token') or request.cookies.get(f'nino_token_{user}')

    if not is_auth(user, token):
        return alert('请先登录', f'/data?user={user}')

    file = request.files['memory_file']
    if file.filename == 'memory.json':
        paths = data._user_paths(user)
        file.save(paths['memory'])
        return redirect(f'/data?user={user}')
    else:
        return alert('请上传正确的文件', f'/data?user={user}')


@shell.route('/import-context', methods=['POST'])
def import_context():
    '''导入上下文'''
    user = request.form.get('user')
    token = request.form.get('token') or request.cookies.get(f'nino_token_{user}')

    if not is_auth(user, token):
        return alert('请先登录', f'/data?user={user}')

    file = request.files['context_file']
    if file.filename == 'context.json':
        paths = data._user_paths(user)
        file.save(paths['context'])
        return redirect(f'/data?user={user}')
    else:
        return alert('请上传正确的文件', f'/data?user={user}')


if __name__ == '__main__':
    # 启动 OneBot 客户端
    onebot.start_onebot_client()

    # 启动 Flask 服务器
    shell.run(host="0.0.0.0", port=5000)
