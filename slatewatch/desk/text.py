"""Display terms and exact programme time formatting."""

from html import escape

KINDS = {
    'black': '黑场', 'freeze': '冻帧', 'silence': '静音', 'clip': '削顶',
    'picture_gap': '画面缺口', 'sound_gap': '伴音缺口',
}
STATUS = {
    'pending': '待审', 'expected': '制作意图覆盖', 'accepted': '人工通过',
    'held': '人工扣留', 'hold': '扣留', 'pass': '已通过',
}
RECOVERY = {'regenerable': '可再生成', 'verify_only': '只能验证旧内容', 'insufficient': '材料不足'}


def safe(value):
    return escape(str(value), quote=True)


def timestamp(value):
    seconds, micros = divmod(value, 1_000_000)
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes:02}:{seconds:02}.{micros:06}'


def interval(value):
    return f'{timestamp(value["start_us"])} – {timestamp(value["end_us"])}'


def lane(hit):
    if hit['channel'] is not None:
        return f'伴音 {hit["channel"]}'
    return '伴音' if hit['kind'] == 'sound_gap' else '画面'


def badge(state):
    return f'<span class="state {state}">{STATUS[state]}</span>'


def options(records, current=''):
    return ''.join(
        f'<option value="{safe(key)}"{" selected" if key == current else ""}>{safe(name)}</option>'
        for key, name in records
    )
