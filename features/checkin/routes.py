"""HTTP routes for checkin."""
from flask import jsonify, render_template, request

from .client import CheckinError
from .scheduler import CheckinTaskError


def register_checkin_routes(app, checkin_scheduler, checkin_beans, checkin_teams, unified):
    @app.get('/checkin')
    def checkin_page():
        return render_template('checkin.html')

    @app.errorhandler(CheckinTaskError)
    def checkin_task_error(error):
        return jsonify(error=str(error)), error.status

    @app.get('/api/checkin/status')
    def checkin_status():
        if login_id := request.args.get('login_id'):
            checkin_scheduler.refresh_account(login_id)
        return jsonify(**checkin_scheduler.status(), team_awards=checkin_teams.award_status())

    @app.route('/api/checkin/beans', methods=['GET', 'POST'])
    def checkin_bean_rewards():
        if request.method == 'GET':
            login_id, kind, item_id, cost = request.args.get('login_id'), None, None, None
        else:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify(error='请提供要操作的任务或奖品。'), 400
            login_id, kind = payload.get('login_id'), payload.get('kind')
            item_id, cost = payload.get('item_id'), payload.get('cost')
            if (kind not in ('claim', 'redeem') or not isinstance(item_id, str) or not item_id
                    or kind == 'redeem' and not isinstance(cost, str)):
                return jsonify(error='请选择并确认要操作的任务或奖品。'), 400
        if not isinstance(login_id, str) or not login_id:
            return jsonify(error='请选择共享账号。'), 400
        try:
            return jsonify(checkin_beans.run(login_id, kind=kind, item_id=item_id, cost=cost))
        except CheckinError as error:
            return jsonify(error=str(error), code=error.code, uncertain=error.uncertain), error.status

    @app.post('/api/checkin/teams/awards')
    def checkin_team_awards():
        return jsonify(queued=checkin_teams.queue_awards(immediate=True))

    @app.route('/api/checkin/beans/tasks', methods=['GET', 'POST'])
    def checkin_bean_tasks():
        if request.method == 'POST':
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify(error='请提供有效的任务执行参数。'), 400
            login_id = payload.get('login_id')
            if login_id is not None and (not isinstance(login_id, str) or unified.get_id(login_id) is None):
                return jsonify(error='请选择有效共享账号。'), 400
            return jsonify(queued=checkin_beans.queue(login_id))
        return jsonify(checkin_beans.task_status())

    @app.post('/api/checkin/teams/preview')
    def preview_checkin_teams():
        return jsonify(checkin_teams.preview())

    @app.post('/api/checkin/teams/join')
    def join_checkin_teams():
        return jsonify(checkin_teams.apply(request.get_json().get('plan_id')))

    @app.post('/api/checkin/settings')
    def save_checkin_settings():
        return jsonify(checkin_scheduler.update_settings(request.get_json()))

    @app.post('/api/checkin/tasks/save')
    def save_checkin_tasks():
        return jsonify(message=checkin_scheduler.save(request.get_json()))

    @app.post('/api/checkin/tasks/stop')
    def stop_checkin_tasks():
        checkin_scheduler.stop(request.get_json().get('ids'))
        return jsonify(message='已停止所选任务，已发出的请求会保留结果')

    @app.post('/api/checkin/run')
    def run_checkin():
        data = request.get_json()
        if data.get('kind') != 'signin':
            return jsonify(error='此入口仅执行每日签到。'), 400
        return jsonify(message=checkin_scheduler.run(data.get('login_ids')))
