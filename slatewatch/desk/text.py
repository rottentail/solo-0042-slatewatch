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
VERDICTS = {
    'same_frame': '同画格同时标', 'timestamp_offset': '同内容·原时标不同',
    'different_content': '内容不同', 'left_missing': '左侧无画面',
    'right_missing': '右侧无画面', 'both_missing': '两侧无画面',
}
FLAGS = {
    'left_long_frame': '左侧长格', 'right_long_frame': '右侧长格',
    'left_hole': '左侧短洞', 'right_hole': '右侧短洞', 'timestamp_offset': '原时标不同',
}


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
