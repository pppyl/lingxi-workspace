"""HTTP routes for monthly coupon."""
from flask import jsonify, render_template, request

from .auth import MonthlyCouponAuthError
from .scheduler import MonthlyTaskError


def register_monthly_coupon_routes(app, monthly_scheduler):
    @app.get('/monthly-coupon')
    def monthly_coupon_page():
        return render_template('monthly_coupon.html')

    @app.errorhandler(MonthlyTaskError)
    @app.errorhandler(MonthlyCouponAuthError)
    def monthly_coupon_error(error):
        return jsonify(error=str(error)), error.status

    @app.get('/api/monthly-coupon/status')
    def monthly_coupon_status():
        return jsonify(monthly_scheduler.status())

    @app.post('/api/monthly-coupon/run')
    def run_monthly_coupon():
        return jsonify(message=monthly_scheduler.run(request.get_json().get('login_ids')))

    @app.post('/api/monthly-coupon/settings')
    def save_monthly_coupon_settings():
        return jsonify(message=monthly_scheduler.settings(request.get_json()))

    @app.post('/api/monthly-coupon/page')
    def save_monthly_coupon_page():
        return jsonify(monthly_scheduler.update_page(request.get_json()))
