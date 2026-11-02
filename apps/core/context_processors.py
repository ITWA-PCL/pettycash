def notifications(request):
    if not request.user.is_authenticated:
        return {"unread_notifications": [], "unread_notification_count": 0}
    from .models import Notification
    unread = Notification.objects.filter(recipient=request.user, is_read=False)
    return {"unread_notifications": unread[:5], "unread_notification_count": unread.count()}
