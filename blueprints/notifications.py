"""Centre de notifications de l'utilisateur connecté."""

from flask import Blueprint, flash, redirect, render_template, request, session, url_for


def creer_blueprint_notifications(deps):
    bp = Blueprint('notifications', __name__)
    login_required = deps['login_required']
    get_all_notifications = deps['get_all_notifications']
    mark_all_read = deps['mark_all_read']

    def _panel_mode():
        return (request.args.get('panel') == '1'
                or request.form.get('panel') == '1'
                or request.headers.get('X-Activity-Panel') == '1')

    @bp.route('/notifications')
    @login_required
    def notifications():
        notifs = get_all_notifications(session.get('user_id'), limit=30)
        if _panel_mode():
            return render_template(
                'notifications_panel.html', notifications=notifs,
                unread_count=sum(1 for n in notifs if not n.get('is_read')),
            )
        return render_template('notifications.html', notifications=notifs)

    @bp.route('/notifications/mark-read', methods=['POST'])
    @login_required
    def mark_notifications_read():
        mark_all_read(session.get('user_id'))
        if _panel_mode():
            return redirect(url_for('.notifications', panel=1))
        flash('Notifications marquées comme lues.', 'success')
        return redirect(url_for('.notifications'))

    return bp
