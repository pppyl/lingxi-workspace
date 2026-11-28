"""Windows desktop entry: default browser, LAN service and notification tray."""
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
import sys
import threading
from urllib.request import Request,ProxyHandler,build_opener

startup_log_path=None


def main():
    global startup_log_path
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--port',type=int)
    interface=parser.add_mutually_exclusive_group()
    interface.add_argument('--headless',action='store_true',help='Run the local service without the tray (verification).')
    interface.add_argument('--open',dest='open_browser',action='store_true',help='Open the workbench in the default browser.')
    parser.add_argument('--quit',action='store_true',help='Exit this user data directory\'s running client.')
    options=parser.parse_args()
    if options.data_dir:os.environ['LINGXI_DATA_DIR']=str(options.data_dir.resolve())
    from runtime_paths import ROOT,DATA_DIR,VERSION,prepare_runtime_data
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    handler=RotatingFileHandler(DATA_DIR/'desktop.log',maxBytes=2*1024*1024,backupCount=2,encoding='utf-8')
    logging.basicConfig(handlers=[handler],level=logging.WARNING)
    startup_log_path=DATA_DIR/'desktop.log'
    logger=logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.info('Desktop startup: version=%s; executable=%s; data=%s',VERSION,sys.executable,DATA_DIR)
    control_file=DATA_DIR/'desktop-instance.json'

    def running_command(command):
        info=json.loads(control_file.read_text('utf-8'))
        request=Request(f'http://127.0.0.1:{int(info["port"])}/_desktop/{command}',data=b'{}',
            headers={'Content-Type':'application/json','Authorization':'Bearer '+info['token']})
        with build_opener(ProxyHandler({})).open(request,timeout=10) as response:return json.load(response)

    if options.quit:
        running_command('quit');return
    lock=(DATA_DIR/'desktop.lock').open('a+b')
    lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        lock.close()
        if json.loads(control_file.read_text('utf-8')).get('version')!=VERSION:
            ctypes.windll.user32.MessageBoxW(None,'请先从托盘退出旧版本客户端，再运行新版本。账号和任务会保留。','本地工具台',0x40)
            return
        if options.open_browser:running_command('open')
        return
    prepare_runtime_data()
    from app import app
    app.config['SESSION_COOKIE_NAME']='lingxi_desktop_session'
    app.extensions['recharge_app'].config['SESSION_COOKIE_NAME']='lingxi_desktop_recharge_session'
    from flask import Response,jsonify,request
    from workspace.network_access import create_http_server
    from workspace.clients.desktop_browser import DefaultBrowserWindow,wait_for_page
    quit_event=threading.Event();token=secrets.token_urlsafe(32);icon=None
    def update_port(port):
        updated={**instance,'port':port}
        temporary=control_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(updated),'utf-8')
        temporary.replace(control_file)
        with window.condition:
            window.url=f'http://127.0.0.1:{port}/?desktop=1'
            window.pending_until=0
            window.condition.notify_all()

    server=create_http_server(app,options.port,fallback_if_occupied=True,on_port_changed=update_port)
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
        current_port=int(request.environ['SERVER_PORT'])
        def events():
            window.attach(client_id);revision=window.focus_revision
            try:
                yield 'data: '+json.dumps({'focus':revision})+'\n\n'
                while not quit_event.is_set() and current_port==server.effective_port:
                    with window.condition:
                        window.condition.wait_for(lambda: revision!=window.focus_revision or
                                                  current_port!=server.effective_port,timeout=10)
                        revision=window.focus_revision
                    if current_port!=server.effective_port:break
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
    try:
        threading.Thread(target=server.run,daemon=True,name='desktop-http').start()
        wait_for_page(f'http://127.0.0.1:{port}/_desktop/info')
        app.extensions['update_checker'].start()
        app.extensions['session_maintenance'].start()
        app.extensions['lingxi_scheduler'].start()
        app.extensions['checkin_scheduler'].start()
        app.extensions['monthly_coupon_scheduler'].start()
        app.extensions['star_day_scheduler'].start()
        app.extensions['net_age_surprise_scheduler'].start()
        app.extensions['platform_manager'].start()
        app.extensions['recharge_app'].extensions['scheduler'].start()
        if options.headless:quit_event.wait()
        else:
            if options.open_browser:open_window()
            import pystray
            from PIL import Image
            icon=pystray.Icon('LingxiWorkspace',Image.open(ROOT/'static/app.ico'),
                f'本地工具台 v{VERSION}（关闭窗口后任务仍在运行）',pystray.Menu(
                    pystray.MenuItem('打开工具台',open_window,default=True),
                    pystray.MenuItem('打开数据目录',lambda *_:os.startfile(DATA_DIR)),
                    pystray.MenuItem('退出客户端',lambda *_:quit_event.set())))
            def watch_exit():
                quit_event.wait();icon.stop()
            threading.Thread(target=watch_exit,daemon=True).start()
            icon.run()
    finally:
        app.extensions['update_checker'].shutdown()
        app.extensions['session_maintenance'].stop()
        app.extensions['lingxi_scheduler'].shutdown()
        app.extensions['checkin_scheduler'].shutdown()
        app.extensions['monthly_coupon_scheduler'].shutdown()
        app.extensions['star_day_scheduler'].shutdown()
        app.extensions['net_age_surprise_scheduler'].shutdown()
        app.extensions['platform_manager'].shutdown()
        app.extensions['recharge_app'].extensions['scheduler'].stop()
        server.close()
        control_file.unlink(missing_ok=True)
        lock.close()


if __name__=='__main__':
    try:main()
    except Exception as error:
        logging.exception('Desktop startup or shutdown failed')
        detail=f'{type(error).__name__}: {error}'
        message='客户端运行失败。\n\n'+detail[:600]
        message+=('\n\n完整错误日志：'+str(startup_log_path) if startup_log_path else '\n\n错误发生在日志文件创建前。')
        ctypes.windll.user32.MessageBoxW(None,message,'本地工具台',0x10)
        raise SystemExit(1)
