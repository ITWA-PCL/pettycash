"""Serve signature images through application access checks, never public media."""
import mimetypes

from django.http import FileResponse, Http404


def signature_response(user):
    if user is None or not user.signature:
        raise Http404('ไม่พบลายเซ็น')
    content_type = mimetypes.guess_type(user.signature.name)[0]
    if content_type not in ('image/png', 'image/jpeg'):
        raise Http404('ไม่รองรับไฟล์ลายเซ็นชนิดนี้')
    try:
        source = user.signature.open('rb')
    except FileNotFoundError:
        raise Http404('ไม่พบไฟล์ลายเซ็น') from None
    response = FileResponse(source, content_type=content_type)
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
