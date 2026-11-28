"""HTTP routes for lingxi tasks."""
from flask import jsonify, render_template, request

from .scheduler import TaskError


def register_lingxi_tasks_routes(app, lingxi_scheduler):
    @app.get('/lingxi/tasks')
    def lingxi_tasks_page():
        return render_template('lingxi_tasks.html')

    @app.errorhandler(TaskError)
    def lingxi_task_error(error):
        return jsonify(error=str(error)),error.status

    @app.get('/api/lingxi/tasks')
    def lingxi_tasks():
        return jsonify(tasks=lingxi_scheduler.snapshots(),warning=lingxi_scheduler.warning,
                       server_time=lingxi_scheduler.now().isoformat(),settings=lingxi_scheduler.settings())

    @app.get('/api/lingxi/settings')
    def lingxi_settings():
        return jsonify(settings=lingxi_scheduler.settings())

    @app.post('/api/lingxi/settings')
    def save_lingxi_settings():
        return jsonify(settings=lingxi_scheduler.update_settings(request.get_json()),message='发送设置已保存并立即生效')

    @app.get('/api/lingxi/tasks/<task_id>')
    def lingxi_task_detail(task_id):
        tasks=lingxi_scheduler.snapshots(task_id)
        if not tasks:raise TaskError('任务不存在。',404)
        return jsonify(task=tasks[0])

    @app.post('/api/lingxi/tasks/save')
    def save_lingxi_task():
        ids=lingxi_scheduler.save(request.get_json())
        return jsonify(ids=ids,message='已保存灵犀定时发送任务')

    @app.post('/api/lingxi/tasks/stop')
    def stop_lingxi_task():
        lingxi_scheduler.stop(request.get_json().get('id'))
        return jsonify(message='已停止后续发送，已发出的请求继续保留回复')

    @app.post('/api/lingxi/tasks/stop-batch')
    def stop_lingxi_tasks():
        ids=lingxi_scheduler.stop_batch(request.get_json())
        return jsonify(ids=ids,message=f'已停止 {len(ids)} 项任务；正在接收的回复会保留，结束后可删除')

    @app.post('/api/lingxi/tasks/adjust')
    def adjust_lingxi_tasks():
        ids=lingxi_scheduler.adjust_batch(request.get_json())
        return jsonify(ids=ids,message=f'已调整 {len(ids)} 项任务')

    @app.post('/api/lingxi/tasks/delete')
    def delete_lingxi_task():
        lingxi_scheduler.delete(request.get_json().get('id'))
        return jsonify(message='任务已删除')

    @app.post('/api/lingxi/tasks/delete-batch')
    def delete_lingxi_tasks():
        ids=lingxi_scheduler.delete_batch(request.get_json())
        return jsonify(ids=ids,message=f'已删除 {len(ids)} 项任务及执行记录')
