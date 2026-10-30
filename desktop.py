"""Windows desktop entry: default browser, loopback service and notification tray."""
from __future__ import annotations
import argparse
import ctypes
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
from pathlib import Path
import secrets
import socket
import threading
from urllib.request import Request,urlopen


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--port',type=int,default=5500)
    parser.add_argument('--headless',action='store_true',help='Run the local service without a window (verification).')
    parser.add_argument('--quit',action='store_true',help='Exit this user data directory\'s running client.')
    options=parser.parse_args()
    if options.data_dir:os.environ['LINGXI_DATA_DIR']=str(options.data_dir.resolve())
    from runtime_paths import ROOT,DATA_DIR,VERSION
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    control_file=DATA_DIR/'desktop-instance.json'

    def running_command(command):
        info=json.loads(control_file.read_text('utf-8'))
        request=Request(f'http://127.0.0.1:{int(info["port"])}/_desktop/{command}',data=b'{}',
            headers={'Content-Type':'application/json','Authorization':'Bearer '+info['token']})
        with urlopen(request,timeout=10) as response:return json.load(response)

    if options.quit:
        running_command('quit');return
    lock=(DATA_DIR/'desktop.lock').open('a+b')
    lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        if json.loads(control_file.read_text('utf-8')).get('version')!=VERSION:
            ctypes.windll.user32.MessageBoxW(None,'请先从托盘退出旧版本客户端，再运行新版本。账号和任务会保留。','本地工具台',0x40)
            return
        running_command('open');return
    handler=RotatingFileHandler(DATA_DIR/'desktop.log',maxBytes=2*1024*1024,backupCount=2,encoding='utf-8')
    logging.basicConfig(handlers=[handler],level=logging.WARNING)
    os.environ['CMCC_NODE_PATH']=str(ROOT/'runtime/node/node.exe')
    if not Path(os.environ['CMCC_NODE_PATH']).is_file():
        raise RuntimeError('客户端运行组件缺失，请完整解压 ZIP 后启动，不要单独移动 EXE。')
    from app import app
    app.config['SESSION_COOKIE_NAME']='lingxi_desktop_session'
    app.extensions['recharge_app'].config['SESSION_COOKIE_NAME']='lingxi_desktop_recharge_session'
    from flask import Response,jsonify,request
    from waitress import create_server
    from desktop_browser import DefaultBrowserWindow,wait_for_page
    quit_event=threading.Event();token=secrets.token_urlsafe(32);icon=None
    port=options.port
    if port:
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1',port))==0:port=0
    server=create_server(app,host='127.0.0.1',port=port,threads=32,channel_timeout=240)
    port=server.effective_port;url=f'http://127.0.0.1:{port}/?desktop=1'
    window=DefaultBrowserWindow(url)

    def open_window(*_):
        if options.headless:return
        window.open()

    @app.get('/_desktop/window-events')
    def window_events():
        client_id=request.args.get('id','')
        if len(client_id)!=32 or any(char not in '0123456789abcdef' for char in client_id):
            return jsonify(error='窗口标识无效'),400
        def events():
            window.attach(client_id);revision=window.focus_revision
            try:
                yield 'data: '+json.dumps({'focus':revision})+'\n\n'
                while not quit_event.is_set():
                    with window.condition:
                        window.condition.wait(timeout=10);revision=window.focus_revision
                    yield 'data: '+json.dumps({'focus':revision})+'\n\n'
            finally:window.detach(client_id)
        return Response(events(),mimetype='text/event-stream')

    @app.post('/_desktop/window-closed')
    def window_closed():
        window.detach(request.get_json().get('id'));return jsonify(ok=True)

    @app.post('/_desktop/<command>')
    def desktop_command(command):
        if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+token):
            return jsonify(error='客户端控制凭据无效'),403
        if command=='quit':quit_event.set()
        elif command=='open':open_window()
        else:return jsonify(error='未知客户端指令'),404
        return jsonify(ok=True)

    @app.get('/_desktop/info')
    def desktop_info():return jsonify(product='lingxi-workspace',version=VERSION,tray=bool(icon and icon.visible),
                                      browser='system-default',window_clients=len(window.clients),opens=window.opens)

    instance={'pid':os.getpid(),'port':port,'token':token,'version':VERSION}
    control_file.write_text(json.dumps(instance),'utf-8')
    threading.Thread(target=server.run,daemon=True,name='desktop-http').start()
    wait_for_page(url)
    app.extensions['session_maintenance'].start()
    app.extensions['lingxi_scheduler'].start()
    app.extensions['checkin_scheduler'].start()
    app.extensions['monthly_coupon_scheduler'].start()
    app.extensions['recharge_app'].extensions['scheduler'].start()
    try:
        if options.headless:quit_event.wait()
        else:
            open_window()
            import pystray
            from PIL import Image
            icon=pystray.Icon('LingxiWorkspace',Image.open(ROOT/'desktop_assets/app.ico'),
                f'本地工具台 v{VERSION}（关闭窗口后任务仍在运行）',pystray.Menu(
                    pystray.MenuItem('打开工具台',open_window,default=True),
                    pystray.MenuItem('打开数据目录',lambda *_:os.startfile(DATA_DIR)),
                    pystray.MenuItem('退出客户端',lambda *_:quit_event.set())))
            def watch_exit():
                quit_event.wait();icon.stop()
            threading.Thread(target=watch_exit,daemon=True).start()
            icon.run()
    finally:
        app.extensions['session_maintenance'].stop()
        app.extensions['lingxi_scheduler'].shutdown()
        app.extensions['checkin_scheduler'].shutdown()
        app.extensions['monthly_coupon_scheduler'].shutdown()
        app.extensions['recharge_app'].extensions['scheduler'].stop()
        server.close()
        control_file.unlink(missing_ok=True)
        lock.close()


if __name__=='__main__':
    try:main()
    except Exception:
        logging.exception('Desktop startup or shutdown failed')
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,'客户端启动失败。请完整解压发布包，并查看数据目录中的 desktop.log。',
            '本地工具台',0x10)
