from datetime import date

from django import forms
from django.utils.html import format_html

from apps.core.thai_dates import thai_month_year


class ThaiMonthWidget(forms.TextInput):
    def format_value(self, value):
        return value.strftime('%Y-%m') if isinstance(value, date) else (value or '')

    def render(self, name, value, attrs=None, renderer=None):
        attrs = dict(attrs or {})
        field_id = attrs.get('id', 'id_' + name)
        value = self.format_value(value)
        try:
            caption = thai_month_year(date.fromisoformat(value + '-01')) if value else ''
        except ValueError:
            caption = ''
        hidden = forms.HiddenInput().render(name, value, attrs=attrs, renderer=renderer)
        return format_html(
            '<div class="thai-month-picker" data-thai-month-picker>{}'
            '<input type="text" id="{}_display" class="thai-month-display" value="{}" '
            'placeholder="เลือกเดือน/ปี" aria-label="ค่าใช้จ่ายของเดือน" autocomplete="off" inputmode="none" {}>'
            '<button type="button" class="thai-month-open" aria-label="เปิดปฏิทินเดือน" aria-expanded="false">'
            '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="16" rx="2"/>'
            '<path d="M8 3v4M16 3v4M4 11h16"/></svg></button></div>',
            hidden, field_id, caption, 'required' if self.is_required else '',
        )

    def id_for_label(self, id_):
        return id_ + '_display' if id_ else ''

    def value_from_datadict(self, data, files, name):
        if name not in data and (name + '_0' in data or name + '_1' in data):
            try:
                return date(int(data[name + '_1']) - 543, int(data[name + '_0']), 1).strftime('%Y-%m')
            except (KeyError, ValueError):
                return 'invalid'
        return super().value_from_datadict(data, files, name)


class ThaiMonthField(forms.DateField):
    widget = ThaiMonthWidget

    def __init__(self, **kwargs):
        super().__init__(input_formats=('%Y-%m',), **kwargs)
