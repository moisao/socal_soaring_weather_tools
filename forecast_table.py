"""The point forecast, drawn: two tables of altitude against hour.

One for thermals (top of lift, cloud, and the expected updraft at each
altitude) and one for wind (direction and speed at each altitude), each
its own image. Two rather than one because Telegram scales a photo down
to 1280 px on its long side: stacked into a single figure the cells come
out too small to read on a phone, where two portrait tables each get the
full screen.

Drawn with matplotlib's object API, never pyplot, so unlike the Skew-T
there is no global figure state for two requests to trip over."""

import textwrap
from pathlib import Path

import matplotlib
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from config import utc_offset_label
from point_forecast import ALTITUDE_STEP_FT, M_TO_FT, daylight_hours

# Units the tables are in. Altitude is always feet (ALTITUDE_STEP_FT).
WIND_UNIT, KT_TO_WIND = 'mph', 1.150779
LIFT_UNIT, MS_TO_LIFT, LIFT_FORMAT = 'm/s', 1.0, '.1f'
# For 'ft/min': LIFT_UNIT, MS_TO_LIFT, LIFT_FORMAT = 'ft/min', 196.85, '.0f'

# Values the colour scales saturate at, fixed so a shade means the same
# thing from one day's table to the next.
LIFT_SCALE_MS = 4.0
WIND_SCALE_KT = 26.0  # 30 mph

SURFACE, INK, SECONDARY, MUTED, HAIRLINE = '#fcfcfb', '#0b0b0b', '#52514e', '#898781', '#e1e0d9'
# One hue each, light to dark: more is darker. Blue for lift, orange for
# wind so the two tables can't be mistaken for each other, grey for cloud.
LIFT_RAMP = LinearSegmentedColormap.from_list('lift', [
    '#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec', '#5598e7', '#3987e5',
    '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281', '#0d366b'])
WIND_RAMP = LinearSegmentedColormap.from_list('wind', [
    '#fdf0e9', '#fad9c9', '#f4ad8c', '#ee8a5d', '#eb6834', '#cf5422', '#a94118', '#84310f',
    '#5f2209'])
CLOUD_RAMP = LinearSegmentedColormap.from_list('cloud', [SURFACE, '#d6d5cf', '#a4a39c', '#75746f'])

FONTS = {'font.family': ['Fira Sans', 'DejaVu Sans']}

# Layout, in inches. A photo this shape lands on Telegram at about 150
# px to the inch, so the 8.5 pt cell text is ~18 px there.
MARGIN = 0.22
LABEL_WIDTH = 1.42
CELL_WIDTH = 0.6
ROW_HEIGHT = 0.26
CELL_GAP = 0.01
MIN_COLUMNS = 8   # narrower than this and the title has no room
DPI = 200


def _ink_on(fill):
    """Text colour for a filled cell: whichever of white and ink is
    further from the fill in luminance."""
    r, g, b = matplotlib.colors.to_rgb(fill)
    return INK if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.45 else '#ffffff'


class _Table:
    """Rows of cells under a title, laid out top to bottom, then drawn
    in one go once the height they add up to is known."""

    def __init__(self, n_columns):
        self.n_columns = n_columns
        self.rows = []      # (kind, height in rows, payload)
        self.overlays = []  # callables drawing over the finished grid

    def row(self, label, cells, label_color=SECONDARY, bold=False):
        """cells: one per column, each None (empty) or a dict with any of
        text, fill, arrow (degrees the wind is *from*)."""
        self.rows.append(('cells', 1.0, (label, cells, label_color, bold)))
        return len(self.rows) - 1

    def heading(self, text):
        self.rows.append(('heading', 1.25, text))

    def gap(self, height=0.35):
        self.rows.append(('gap', height, None))

    def column_x(self, column):
        return MARGIN + LABEL_WIDTH + column * CELL_WIDTH

    def row_y(self, index):
        """Top edge of a row, inches from the top of the grid."""
        return sum(height for _, height, _ in self.rows[:index]) * ROW_HEIGHT

    def draw(self, title, subtitle, footer, path):
        grid_width = LABEL_WIDTH + max(self.n_columns, MIN_COLUMNS) * CELL_WIDTH
        grid_height = sum(height for _, height, _ in self.rows) * ROW_HEIGHT
        # Wrapped by character count: ~16 of the footer's 7.5 pt characters
        # to the inch, which is as exact as a note needs.
        footer = [wrapped for line in footer for wrapped in textwrap.wrap(line, int(grid_width * 16))]
        header, footer_height = 0.72, 0.16 + 0.17 * len(footer)
        width = grid_width + 2 * MARGIN
        height = header + grid_height + footer_height + MARGIN

        fig = Figure(figsize=(width, height), dpi=DPI, facecolor=SURFACE)
        FigureCanvasAgg(fig)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, width)
        ax.set_ylim(height, 0)  # inches, y down: every position below is a real size
        ax.axis('off')

        ax.text(MARGIN, 0.33, title, fontsize=13, fontweight='bold', color=INK, va='baseline')
        ax.text(MARGIN, 0.58, subtitle, fontsize=8.5, color=SECONDARY, va='baseline')

        self.origin_y = header
        for index, (kind, _, payload) in enumerate(self.rows):
            y = header + self.row_y(index)
            middle = y + ROW_HEIGHT / 2
            if kind == 'heading':
                ax.text(MARGIN, y + ROW_HEIGHT * 0.8, payload, fontsize=9, fontweight='bold',
                        color=INK, va='center')
            elif kind == 'cells':
                label, cells, label_color, bold = payload
                ax.text(MARGIN + LABEL_WIDTH - 0.1, middle, label, fontsize=8, color=label_color,
                        fontweight='bold' if bold else 'normal', ha='right', va='center')
                for column, cell in enumerate(cells):
                    if cell is not None:
                        self._cell(ax, self.column_x(column), y, cell)
        for overlay in self.overlays:
            overlay(ax)

        y = header + grid_height + 0.2
        for line in footer:
            ax.text(MARGIN, y, line, fontsize=7.5, color=MUTED, va='center')
            y += 0.17
        fig.savefig(path, dpi=DPI, facecolor=SURFACE)

    @staticmethod
    def _cell(ax, x, y, cell):
        fill = cell.get('fill')
        if fill is not None:
            ax.add_patch(Rectangle((x + CELL_GAP, y + CELL_GAP), CELL_WIDTH - 2 * CELL_GAP,
                                   ROW_HEIGHT - 2 * CELL_GAP, facecolor=fill, edgecolor='none'))
        color = cell.get('color') or (_ink_on(fill) if fill is not None else INK)
        middle = y + ROW_HEIGHT / 2
        text_x, align = x + CELL_WIDTH / 2, 'center'
        if cell.get('arrow') is not None:
            # Tail to head along the way the air is moving, i.e. away from
            # the direction the wind is named for. y runs down the page.
            angle = np.radians(cell['arrow'])
            dx, dy = -np.sin(angle) * 0.075, np.cos(angle) * 0.075
            cx = x + 0.17
            ax.annotate('', xy=(cx + dx, middle + dy), xytext=(cx - dx, middle - dy),
                        arrowprops={'arrowstyle': '-|>', 'color': color, 'linewidth': 0.9,
                                    'mutation_scale': 6.5, 'shrinkA': 0, 'shrinkB': 0})
            text_x, align = x + CELL_WIDTH - 0.07, 'right'
        if cell.get('text'):
            ax.text(text_x, middle, cell['text'], fontsize=cell.get('size', 8.5), color=color,
                    fontweight=cell.get('weight', 'normal'), ha=align, va='center')


def _feet(metres, to=50):
    return f'{round(metres * M_TO_FT / to) * to:,.0f}'


def _hour_label(dt):
    return f'{dt:%I}'.lstrip('0') + f'{dt:%p}'.lower()


def _clock(dt):
    return f'{dt:%I:%M}'.lstrip('0') + f'{dt:%p}'.lower()


def _columns(forecast):
    """(valid time, Hour or None) for every hour the sun is up. An hour
    the model has no output for -- GFS past f120 is 3-hourly -- keeps its
    column, left empty, rather than the next one closing the gap: the
    columns stay an hour apart, so a 3-hour step reads as one."""
    by_valid = {hour.valid: hour for hour in forecast.hours}
    valid_times = set(daylight_hours(forecast.lat, forecast.lon, forecast.day)) | by_valid.keys()
    return [(valid, by_valid.get(valid)) for valid in sorted(valid_times)]


def _each(columns, cell):
    """One cell per column: cell(hour), or empty where there's no hour."""
    return [None if hour is None else cell(hour) for _, hour in columns]


def _hour_row(table, columns, tz):
    table.row('', [{'text': _hour_label(valid.astimezone(tz)),
                    'color': SECONDARY if hour is not None else HAIRLINE,
                    'weight': 'bold', 'size': 8} for valid, hour in columns])


def _gap_note(forecast, columns):
    if any(hour is None for _, hour in columns):
        return [f'Empty columns: {forecast.source.label} has no output at those hours.']
    return []


def _altitudes(bottom_m, top_m):
    """Table rows from just above the ground to top_m: feet MSL, highest
    first, on the round ALTITUDE_STEP_FT."""
    first = (np.floor(bottom_m * M_TO_FT / ALTITUDE_STEP_FT) + 1) * ALTITUDE_STEP_FT
    return np.arange(first, top_m * M_TO_FT + 1, ALTITUDE_STEP_FT)[::-1]


def _altitude_label(feet):
    """Every row is labelled; the thousands stand out to count from."""
    major = feet % 1000 == 0
    return f'{feet:,.0f}', (INK if major else MUTED), major


def _titles(forecast, tz, name, what):
    place = name or f'{forecast.lat:.3f}, {forecast.lon:.3f}'
    first, last = (run.astimezone(tz) for run in (forecast.runs[0], forecast.runs[-1]))
    zone = f'{last:%Z} ({utc_offset_label(last)})'
    if first == last:
        runs = f'run {last:%a} {_hour_label(last)} {zone}'
    else:
        runs = f'runs {first:%a} {_hour_label(first)}\N{EN DASH}{last:%a} {_hour_label(last)} {zone}'
    sunrise, sunset = forecast.sunrise.astimezone(tz), forecast.sunset.astimezone(tz)
    subtitle = (f'{forecast.day:%a %b} {forecast.day.day}, {forecast.day.year}  \N{MIDDLE DOT}  '
                f'{forecast.source.label}, {runs}  \N{MIDDLE DOT}  '
                f'sun {_clock(sunrise)}\N{EN DASH}{_clock(sunset)}')
    return f'{place}: {what}', subtitle


def _grid_top_m(forecast):
    """How high the wind table goes: comfortably past the day's highest
    thermal, a useful minimum above the ground on a day with none, and
    never past the levels actually fetched."""
    surface = forecast.hours[0].surface_m
    tops = [hour.top_m for hour in forecast.hours if hour.top_m is not None]
    top = max([surface + 4000 / M_TO_FT] + [t + 1500 / M_TO_FT for t in tops])
    top = np.ceil(top * M_TO_FT / 1000) * 1000 / M_TO_FT
    return min(top, forecast.top_level_m)


def _thermal_table(forecast, tz, name, path):
    hours = forecast.hours
    columns = _columns(forecast)
    table = _Table(len(columns))
    _hour_row(table, columns, tz)
    table.row('Top of lift (ft)', _each(columns, lambda hour: (
        {'text': _feet(hour.top_m), 'weight': 'bold'} if hour.top_m is not None
        else {'text': '\N{EN DASH}', 'color': MUTED})), label_color=INK, bold=True)
    table.row('Cumulus base (ft)', _each(columns, lambda hour: (
        {'text': _feet(hour.cu_base_m)} if hour.cu_base_m is not None
        else {'text': 'blue', 'color': MUTED} if hour.top_m is not None else None)))
    for label, layer in (('High cloud (%)', 2), ('Mid cloud (%)', 1), ('Low cloud (%)', 0)):
        table.row(label, _each(columns, lambda hour, layer=layer: (
            None if hour.cloud[layer] is None
            else {'text': f'{hour.cloud[layer]:.0f}', 'fill': CLOUD_RAMP(hour.cloud[layer] / 100)})))

    table.gap()
    table.heading(f'Thermal updraft ({LIFT_UNIT}) by altitude (ft MSL)')
    surface = hours[0].surface_m
    tops = [hour.top_m for hour in hours if hour.top_m is not None]
    top = max(tops + [surface + 1000 / M_TO_FT]) + ALTITUDE_STEP_FT / M_TO_FT
    lift_altitudes = _altitudes(surface, top)
    first_lift_row = len(table.rows)
    for feet in lift_altitudes:
        metres = feet / M_TO_FT
        cells = []
        for _, hour in columns:
            if hour is None:
                cells.append(None)
                continue
            lift = hour.lift_at(metres)
            in_layer = hour.top_m is not None and metres < hour.top_m
            if hour.cu_base_m is not None and 0 <= metres - hour.cu_base_m < ALTITUDE_STEP_FT / M_TO_FT:
                cells.append({'text': '\N{CLOUD}', 'color': MUTED, 'size': 10})
            elif lift is None:
                # No heat flux from the model to size the thermals with:
                # show where they reach, without a strength.
                cells.append({'fill': HAIRLINE} if in_layer else None)
            elif lift * MS_TO_LIFT >= 0.05 * MS_TO_LIFT and in_layer:
                cells.append({'text': format(lift * MS_TO_LIFT, LIFT_FORMAT),
                              'fill': LIFT_RAMP(min(lift / LIFT_SCALE_MS, 1.))})
            else:
                cells.append(None)
        label, color, major = _altitude_label(feet)
        table.row(label, cells, label_color=color, bold=major)
    table.row(f'ground {_feet(surface, 10)}', [None] * len(columns), label_color=MUTED)
    table.gap(0.15)
    _hour_row(table, columns, tz)
    launch = _launch_above_ground(forecast)
    if launch is not None:
        table.overlays.append(_launch_rule(table, first_lift_row, lift_altitudes, launch))

    footer = ['Updraft is the air in a thermal core (w* with Lenschow\'s height profile); '
              'your climb is that less your sink rate.',
              'Top of lift and cumulus base are the surface parcel\'s, as on the sounding.  '
              '\N{CLOUD} cumulus base  \N{MIDDLE DOT}  blue = thermals without cumulus  '
              '\N{MIDDLE DOT}  ground is the model\'s terrain.']
    if any(hour.wstar is None for hour in hours):
        footer.append('Grey cells: thermals reach here, but the model gave no heat flux to '
                      'size them with.')
    footer += _gap_note(forecast, columns)
    title, subtitle = _titles(forecast, tz, name, 'thermals')
    table.draw(title, subtitle, footer, path)


def _launch_above_ground(forecast):
    """The launch elevation, if it is known and above the model's ground
    -- below it, the model has no air at launch height to read."""
    launch = forecast.launch_m
    return launch if launch is not None and launch > forecast.hours[0].surface_m else None


def _launch_rule(table, first_row, altitudes, launch_m):
    """An overlay drawing a hairline across the grid at launch height,
    labelled in the margin, so the rows above and below launch read
    apart. Snapped to the gap between rows like the top-of-lift rule."""
    def draw(ax):
        feet = launch_m * M_TO_FT
        if not altitudes[-1] <= feet <= altitudes[0]:
            return
        rows_above = np.ceil((altitudes[0] - feet) / ALTITUDE_STEP_FT)
        y = table.origin_y + table.row_y(first_row) + rows_above * ROW_HEIGHT
        x0, x1 = table.column_x(0), table.column_x(table.n_columns)
        ax.plot([x0, x1], [y, y], color=SECONDARY, linewidth=1.2, solid_capstyle='butt')
        ax.text(MARGIN, y, f'launch {_feet(launch_m, 10)}', fontsize=7, color=SECONDARY,
                ha='left', va='center')
    return draw


def _wind_cell(direction, speed_kt):
    return {'text': f'{speed_kt * KT_TO_WIND:.0f}', 'arrow': direction,
            'fill': WIND_RAMP(min(speed_kt / WIND_SCALE_KT, 1.))}


def _wind_table(forecast, tz, name, path):
    columns = _columns(forecast)
    table = _Table(len(columns))
    _hour_row(table, columns, tz)
    surface = forecast.hours[0].surface_m
    launch = _launch_above_ground(forecast)
    # With a launch elevation, the free-air rows start above it: the
    # launch row below stands for everything from the ground up to launch.
    altitudes = _altitudes(launch if launch is not None else surface, _grid_top_m(forecast))
    first_row = len(table.rows)
    for feet in altitudes:
        cells = []
        for _, hour in columns:
            wind = None if hour is None else hour.wind_at(feet / M_TO_FT)
            cells.append(None if wind is None else _wind_cell(*wind))
        label, color, major = _altitude_label(feet)
        table.row(label, cells, label_color=color, bold=major)
    table.gap(0.15)

    def gust_cell(gust_kt):
        return None if gust_kt is None else {
            'text': f'{gust_kt * KT_TO_WIND:.0f}', 'fill': WIND_RAMP(min(gust_kt / WIND_SCALE_KT, 1.))}

    if launch is not None:
        # The model's 10 m wind, not its free air at launch height: on the
        # stations at Little Black (Holfuy and WeatherXM, 2026-10-16/17)
        # the 10 m wind was the closer of the two in three comparisons out
        # of four, where the free air carried the models' morning jet.
        table.row(f'Launch, {_feet(launch, 10)} ft',
                  _each(columns, lambda hour: _wind_cell(*hour.surface_wind)),
                  label_color=INK, bold=True)
        table.row('Gust (estimate)', _each(columns, lambda hour: gust_cell(hour.launch_gust_kt(launch))))
    else:
        table.row(f'Surface, {_feet(surface, 10)} ft',
                  _each(columns, lambda hour: _wind_cell(*hour.surface_wind)), label_color=INK, bold=True)
        table.row('Gust', _each(columns, lambda hour: gust_cell(hour.gust_kt)))
    table.gap(0.15)
    _hour_row(table, columns, tz)

    def top_of_lift(ax):
        # A rule across each column at that hour's top of lift, so the
        # winds a thermal climbs through read apart from those above it.
        step = ALTITUDE_STEP_FT
        for column, (_, hour) in enumerate(columns):
            if hour is None or hour.top_m is None or hour.top_m * M_TO_FT >= altitudes[0] + step:
                continue
            # Snapped to the gap above the highest row still inside the
            # lift (rows run highest first), where it can't strike through
            # a number.
            rows_above = np.ceil((altitudes[0] - hour.top_m * M_TO_FT) / step)
            y = table.origin_y + table.row_y(first_row) + max(rows_above, 0) * ROW_HEIGHT
            x = table.column_x(column)
            ax.plot([x + CELL_GAP, x + CELL_WIDTH - CELL_GAP], [y, y], color=INK, linewidth=2,
                    solid_capstyle='butt')

    table.overlays.append(top_of_lift)
    footer = [f'Wind in {WIND_UNIT}; arrows point the way the air is moving.  '
              '\N{EM DASH} top of lift that hour.']
    if launch is not None:
        footer.append(f'Altitudes are feet MSL. Launch row: the model\'s 10 m wind (its ground is '
                      f'{_feet(surface, 10)} ft); rows above are the free air at that height. Gust is an '
                      f'estimate from the free air at launch height and the model\'s gust-to-wind ratio.')
    else:
        footer.append('Altitudes are feet MSL. Surface is the model\'s terrain, with its 10 m wind.')
    footer += _gap_note(forecast, columns)
    title, subtitle = _titles(forecast, tz, name, 'wind')
    table.draw(title, subtitle, footer, path)


def render_tables(forecast, tz, name=None, output_dir=None):
    """Draw the forecast's two tables. Returns their paths, thermals
    first.

    Named for the newest run in them, so a later run's forecast of the
    same day is a new pair of files rather than overwriting this one."""
    stub = (f'PF_{forecast.source.cache_prefix}_{forecast.lat:.2f}_{forecast.lon:.2f}_'
            f'{forecast.day:%Y%m%d}_run{forecast.runs[-1]:%Y%m%d_%HZ}')
    if output_dir is not None:
        stub = str(Path(output_dir) / stub)
    paths = [Path(f'{stub}_thermals.png'), Path(f'{stub}_wind.png')]
    with matplotlib.rc_context(FONTS):
        _thermal_table(forecast, tz, name, paths[0])
        _wind_table(forecast, tz, name, paths[1])
    return paths
