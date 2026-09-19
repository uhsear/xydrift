#!/usr/bin/env python
"""Name the rows whose stored X and Y columns disagree with their own geometry, and resync only those.

A point layer carries X and Y attribute columns beside its real geometry. Somebody
moves a feature in an edit session. The geometry moves, the two columns do not, and
nothing anywhere reports an error. Every consumer that reads the columns instead of
the shape is now wrong, quietly, until somebody notices a point drawn in the wrong
place from a spreadsheet.

Calculate Geometry Attributes already recomputes those columns, and it is the right
tool when you want every row rewritten. That is also the problem. It writes all of
them unconditionally, so `last_edited_date` and `last_edited_user` are stamped on
every record on every run, and the history that said who moved what is gone. An
attribute rule is the other usual answer: it is better than both, and it repairs
nothing already wrong, because it fires on edit and the drifted rows are not being
edited. This reads the layer, names the rows that actually disagree, and rewrites
only those.

The comparison needs a tolerance and a coordinate system, and the tolerance ruins it
in either direction. Too tight and ordinary float noise reads as drift, so the tool
rewrites every row it was written to spare. Too loose and a real move hides inside
it. Both ends are pinned in the self-test.

    python xydrift.py --self-test
    python xydrift.py --layer prod.sde/Addresses
    python xydrift.py --layer prod.sde/Addresses --tolerance 0.5 --tolerance-units meters
    python xydrift.py --layer prod.sde/Addresses --workspace prod.sde --apply

Exit codes: 0 no drift or resync done, 1 drift found and not written, 2 the resync
failed part way, 64 usage error.
"""

from __future__ import print_function

import argparse
import math
import sys

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Decimal places the geometry is rounded to before it is compared. Eight decimal
# degrees is about 1.1 mm, far below any tolerance worth using, and rounding
# there keeps the last bits of a float64 out of the comparison.
GEOM_DECIMALS = 8

# Tolerance used for a geographic layer when none is given. 1e-6 degrees is about
# 0.11 m at the equator: below any real edit, above any float noise.
DEFAULT_TOLERANCE_DEGREES = 1e-6

# Metres in one degree of latitude, and of longitude at the equator. Used only to
# convert a metre tolerance for a geographic layer.
# ponytail: one constant, no latitude term. A degree of longitude shrinks toward
# the poles, so the converted tolerance is always the tightest one and never
# hides a real move. It does report more ordinary noise the further a layer sits
# from the equator. Take the cosine of the layer's mid-latitude if that bites.
METERS_PER_DEGREE = 111320.0

METERS_PER_FOOT = 0.3048

# Coordinate system the comparison happens in. 4326 is WGS84 lon/lat, which is
# what a stored X/Y column on a public-facing layer almost always holds.
DEFAULT_WKID = 4326

# Rows printed before the report says how many more there are.
DEFAULT_LIMIT = 10

# Attribute column types a coordinate may be compared in. Everything else is
# refused with a reason rather than coerced.
NUMERIC_COORD_TYPES = ("Double", "Single")

# Bits of mantissa in a Single (float32) column, including the implicit bit.
SINGLE_MANTISSA_BITS = 24

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

PRO_PYTHON = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"

# Per-coordinate verdicts.
OK = "OK"
DRIFT = "DRIFT"
FILL = "FILL"
NO_GEOMETRY = "NO_GEOMETRY"

# Row verdicts, worst first. A row takes the worst verdict of its two columns.
VERDICT_ORDER = (NO_GEOMETRY, DRIFT, FILL, OK)


class RowPlan(object):
    """One row's verdict and the values, if any, that would be written to it."""

    def __init__(self, oid, verdict, dx, dy, new_x, new_y):
        self.oid = oid
        self.verdict = verdict
        # Measured distance between the stored column and the geometry, in the
        # comparison system's units. None when there was nothing to measure.
        self.dx = dx
        self.dy = dy
        # None means leave the column alone. This is not the same as writing
        # None, which is how a blind recalculation nulls a column it could not
        # compute.
        self.new_x = new_x
        self.new_y = new_y

    @property
    def writes(self):
        return self.new_x is not None or self.new_y is not None

    def __repr__(self):
        return "RowPlan(%r, %s, dx=%r, dy=%r)" % (
            self.oid, self.verdict, self.dx, self.dy)


# ----------------------------------------------------------------- pure core

def delta(a, b):
    """Plain distance between two coordinates on one axis."""
    return abs(a - b)


def wrapped_delta(a, b):
    """Distance in degrees between two longitudes, the short way round.

    -180 and +180 are the same meridian. A plain subtraction calls them 360
    degrees apart, which reads as the largest drift the layer can hold on the
    one line where nothing moved at all.
    """
    d = abs(a - b) % 360.0
    if d > 180.0:
        d = 360.0 - d
    return d


def classify_pair(stored, new, tolerance, wrap=False):
    """Verdict for one stored coordinate against its geometry-derived value.

    The order of the two null branches is the whole safety argument. Geometry
    first: a feature with no geometry has nothing to say about its columns, so
    the stored value is left exactly as it is. A recalculation that starts from
    the geometry nulls that column instead, and the record loses the only copy
    of the coordinate it had.
    """
    if new is None:
        return NO_GEOMETRY
    if stored is None:
        return FILL
    d = wrapped_delta(stored, new) if wrap else delta(stored, new)
    # Strictly greater, as the source this was taken from has it. A value
    # exactly at the tolerance is inside the tolerance.
    return DRIFT if d > tolerance else OK


def measure(stored, new, wrap=False):
    """Distance between a stored coordinate and its geometry, or None."""
    if stored is None or new is None:
        return None
    return wrapped_delta(stored, new) if wrap else delta(stored, new)


def worst(*verdicts):
    """The verdict a row takes from its two columns."""
    for candidate in VERDICT_ORDER:
        if candidate in verdicts:
            return candidate
    raise ValueError("no verdict given")


def plan_row(oid, stored_x, stored_y, geom_x, geom_y, tolerance, wrap=False):
    """Decide what, if anything, this row needs written to it.

    Each column is decided on its own. A row whose Y drifted and whose X did not
    has only its Y rewritten, because rewriting the column that already agreed
    is an edit nobody asked for and it stamps editor tracking just the same.
    """
    vx = classify_pair(stored_x, geom_x, tolerance, wrap)
    vy = classify_pair(stored_y, geom_y, tolerance, False)
    new_x = geom_x if vx in (DRIFT, FILL) else None
    new_y = geom_y if vy in (DRIFT, FILL) else None
    return RowPlan(oid, worst(vx, vy),
                   measure(stored_x, geom_x, wrap),
                   measure(stored_y, geom_y, False),
                   new_x, new_y)


def rounding_floor(decimals=GEOM_DECIMALS):
    """Smallest difference the rounded geometry can still express."""
    return 10.0 ** -decimals


def single_step(magnitude):
    """Gap between two values a Single column can hold near this magnitude.

    A float32 carries 24 bits of mantissa. Near longitude 82 that is a step of
    2**-17, about 7.6e-6 degrees, which is coarser than the default tolerance.
    A Double column read back from a Single column therefore differs from the
    geometry on every row, on every run, for ever.
    """
    m = abs(float(magnitude))
    if m == 0.0:
        return 0.0
    return 2.0 ** (int(math.floor(math.log(m, 2))) - (SINGLE_MANTISSA_BITS - 1))


def refuse_coord_field(name, kind, tolerance, magnitude):
    """Reason this column cannot hold a comparable coordinate, or None.

    A String column is the one that bites, because it compares without raising.
    "-82.1" and "-82.10" are the same place and two different strings, and the
    tool would report every row in the layer as drifted.
    """
    if kind not in NUMERIC_COORD_TYPES:
        return ("column %s is type %s. A coordinate has to be compared as a "
                "number, and this one is not stored as one." % (name, kind))
    if kind == "Single":
        step = single_step(magnitude)
        if step > tolerance:
            return ("column %s is a Single. Near %g it resolves to %g, which "
                    "is coarser than the tolerance %g, so every row would read "
                    "as drifted." % (name, magnitude, step, tolerance))
    return None


def resolve_tolerance(value, units, geographic, meters_per_unit=None):
    """Tolerance in the comparison system's own units.

    Units are the part that goes wrong silently. Half a metre against a lon/lat
    column is 4.5e-6 degrees, and passing 0.5 straight through would accept half
    a degree of movement, about 55 km, as no drift at all.

    Both branches end at the same rounding floor. The geometry is rounded to
    GEOM_DECIMALS places whatever system it is read in, so a tolerance at or
    below that grid reads the rounding itself as drift, in feet exactly as in
    degrees.
    """
    if value is None:
        raise ValueError("no tolerance given")
    if value <= 0:
        raise ValueError("a tolerance of %g is not a distance" % value)

    if geographic:
        if units in (None, "degrees"):
            tolerance = value
        elif units == "meters":
            tolerance = value / METERS_PER_DEGREE
        elif units == "feet":
            tolerance = value * METERS_PER_FOOT / METERS_PER_DEGREE
        else:
            raise ValueError("unknown tolerance units %r" % (units,))
        label = "degrees"
    elif units == "degrees":
        raise ValueError(
            "a tolerance in degrees means nothing against a projected layer. "
            "Give it in the layer's own linear units, or pass meters.")
    elif units is None:
        tolerance = value
        label = "linear units"
    else:
        if units not in ("meters", "feet"):
            raise ValueError("unknown tolerance units %r" % (units,))
        if not meters_per_unit:
            raise ValueError(
                "the layer does not report metres per unit, so a %s tolerance "
                "cannot be converted. Give the tolerance in the layer's own "
                "units." % units)
        meters = value * METERS_PER_FOOT if units == "feet" else value
        tolerance = meters / meters_per_unit
        label = "linear units"

    floor = rounding_floor()
    if tolerance <= floor:
        raise ValueError(
            "a tolerance of %g %s is at or below the %g grid the geometry is "
            "rounded to, so the rounding itself would read as drift"
            % (tolerance, label, floor))
    return tolerance


def summarize(plans):
    """Count of each verdict, every verdict present as a key."""
    counts = dict((v, 0) for v in VERDICT_ORDER)
    for plan in plans:
        counts[plan.verdict] += 1
    return counts


def to_write(plans):
    """The rows a resync would touch, in the order they were read."""
    return [p for p in plans if p.writes]


def describe(plans, tolerance, units_label, limit=DEFAULT_LIMIT):
    """The report, as lines. No printing here so the self-test can read it."""
    counts = summarize(plans)
    lines = ["rows read: %d" % len(plans),
             "tolerance: %g %s" % (tolerance, units_label)]
    for verdict in VERDICT_ORDER:
        lines.append("  %-12s %6d" % (verdict, counts[verdict]))

    moved = [p for p in plans if p.verdict in (DRIFT, FILL)]
    if not moved:
        lines.append("")
        lines.append("Every stored coordinate agrees with its geometry.")
        return lines

    lines.append("")
    lines.append("rows whose columns disagree with their geometry:")
    for plan in moved[:limit]:
        lines.append("  OID %s %s: dx=%s dy=%s" % (
            plan.oid, plan.verdict, _fmt(plan.dx), _fmt(plan.dy)))
    if len(moved) > limit:
        lines.append("  ... and %d more" % (len(moved) - limit))

    if counts[NO_GEOMETRY]:
        lines.append("")
        lines.append("%d row(s) have no geometry. Their stored coordinates are "
                     "left alone." % counts[NO_GEOMETRY])
    return lines


def _fmt(value):
    return "none" if value is None else "%g" % value


def update_fields(x_field, y_field):
    """Fields the update cursor opens with.

    No geometry token appears here, and that is deliberate. An update cursor
    that carries SHAPE@ can write the shape back, and a rounded geometry token
    written back to a projected feature class moves the point. The geometry is
    read by a separate cursor and never travels on the writing one.
    """
    return ["OID@", x_field, y_field]


# ------------------------------------------------------------------ geodatabase

def _import_arcpy():
    """Import arcpy only when a real feature class is about to be read."""
    try:
        import arcpy
    except ImportError:
        sys.exit(
            "arcpy was not found. Run this with the Python that ships with "
            "ArcGIS Pro:\n"
            '  "%s" xydrift.py\n'
            "or the propy.bat in ...\\Pro\\bin\\Python\\Scripts\\.\n"
            "Only --self-test runs without arcpy." % PRO_PYTHON
        )
    return arcpy


def layer_magnitude(arcpy, layer, geographic):
    """Largest coordinate this layer can hold, for the Single-precision check.

    A geographic layer is bounded by the globe, so 180 is the worst case and no
    read is needed. A projected one has no such bound, and a Single column's
    step grows with the coordinate: near 600000 ft it is 0.0625 ft, coarser
    than any tolerance worth using. The extent is stored metadata, so asking
    for it costs no second pass over the rows.
    """
    if geographic:
        return 180.0
    extent = arcpy.Describe(layer).extent
    corners = [abs(v) for v in
               (extent.XMin, extent.XMax, extent.YMin, extent.YMax)
               if v is not None]
    # An empty layer reports no extent. It also has no row that can be wrong,
    # so a magnitude of zero refuses nothing.
    return max(corners) if corners else 0.0


def layer_profile(arcpy, layer, wkid):
    """The spatial reference to compare in, and the layer's column types."""
    sr = arcpy.SpatialReference(wkid)
    geographic = sr.type == "Geographic"
    meters_per_unit = None if geographic else getattr(sr, "metersPerUnit", None)
    types = dict((f.name, f.type) for f in arcpy.ListFields(layer))
    return sr, geographic, meters_per_unit, types


def scan(arcpy, layer, x_field, y_field, sr, tolerance, wrap):
    """Read every row once and plan it. Opens no update cursor.

    ponytail: every plan is held in memory and there is no where clause, so a
    few hundred thousand rows are fine and tens of millions are not. Chunk by
    OID range if a layer outgrows it.
    """
    fields = ["OID@", x_field, y_field, "SHAPE@X", "SHAPE@Y"]
    plans = []
    with arcpy.da.SearchCursor(layer, fields, spatial_reference=sr) as cursor:
        for oid, sx, sy, gx, gy in cursor:
            # Round before comparing, not after. The geometry token returns the
            # full float64 projection of the point, whose last bits differ from
            # the value that was stored, and comparing those bits is how a
            # tolerance ends up being asked to hide arithmetic.
            gx = None if gx is None else round(gx, GEOM_DECIMALS)
            gy = None if gy is None else round(gy, GEOM_DECIMALS)
            plans.append(plan_row(oid, sx, sy, gx, gy, tolerance, wrap))
    return plans


def resync(arcpy, layer, plans, x_field, y_field, workspace=None):
    """Write the planned coordinates. Returns the number of rows written.

    An edit session is opened only when --workspace is given. A bare update
    cursor cannot commit to a versioned feature class, and an Editor pointed at
    a workspace that does not hold the class fails at startEditing rather than
    writing nothing quietly.
    """
    wanted = dict((p.oid, p) for p in to_write(plans))
    if not wanted:
        return 0
    fields = update_fields(x_field, y_field)
    editor = arcpy.da.Editor(workspace) if workspace else None
    if editor is not None:
        editor.startEditing(False, True)
        editor.startOperation()
    written = 0
    try:
        with arcpy.da.UpdateCursor(layer, fields) as cursor:
            for row in cursor:
                plan = wanted.get(row[0])
                if plan is None:
                    continue
                row = list(row)
                if plan.new_x is not None:
                    row[1] = plan.new_x
                if plan.new_y is not None:
                    row[2] = plan.new_y
                cursor.updateRow(row)
                written += 1
        if editor is not None:
            editor.stopOperation()
            editor.stopEditing(True)
    except Exception:
        if editor is not None:
            editor.abortOperation()
            editor.stopEditing(False)
        raise
    return written


def run(args, arcpy):
    """The whole run once arcpy exists. Separate from main so a stub can drive it."""
    if not arcpy.Exists(args.layer):
        print("error: layer does not exist: %s" % args.layer, file=sys.stderr)
        return 64

    sr, geographic, meters_per_unit, types = layer_profile(
        arcpy, args.layer, args.wkid)

    tolerance = args.tolerance
    units = args.tolerance_units
    if tolerance is None:
        if not geographic:
            print("error: a projected layer has no sane default tolerance. "
                  "Pass --tolerance in the layer's own linear units.",
                  file=sys.stderr)
            return 64
        tolerance = DEFAULT_TOLERANCE_DEGREES
        units = "degrees"
    try:
        tolerance = resolve_tolerance(tolerance, units, geographic,
                                      meters_per_unit)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    for field in (args.x_field, args.y_field):
        if field not in types:
            print("error: column %s is not in %s" % (field, args.layer),
                  file=sys.stderr)
            return 64

    magnitude = layer_magnitude(arcpy, args.layer, geographic)
    for field in (args.x_field, args.y_field):
        reason = refuse_coord_field(field, types[field], tolerance, magnitude)
        if reason is not None:
            print("error: %s" % reason, file=sys.stderr)
            return 64

    units_label = "degrees" if geographic else "linear units"
    plans = scan(arcpy, args.layer, args.x_field, args.y_field, sr,
                 tolerance, geographic)
    for line in describe(plans, tolerance, units_label, args.limit):
        print(line)

    pending = to_write(plans)
    if not pending:
        return 0

    if not args.apply:
        print("")
        print("Check only. Nothing was written. Re-run with --apply to resync "
              "%d row(s)." % len(pending))
        return 1

    print("")
    print("=== APPLY ===")
    try:
        written = resync(arcpy, args.layer, plans, args.x_field, args.y_field,
                         args.workspace)
    except Exception as exc:
        print("  FAILED: %s" % exc, file=sys.stderr)
        return 2
    print("resynced %d row(s)." % written)
    return 0


# ------------------------------------------------------------------ self-test

class _StubField(object):
    """The two attributes of an arcpy field object that this tool reads."""

    def __init__(self, name, kind):
        self.name = name
        self.type = kind


class _StubExtent(object):
    """arcpy's extent object in the four corners this tool reads."""

    def __init__(self, xmin, ymin, xmax, ymax):
        self.XMin = xmin
        self.YMin = ymin
        self.XMax = xmax
        self.YMax = ymax


class _StubDescribe(object):
    """arcpy.Describe's result in the one attribute this tool reads."""

    def __init__(self, extent):
        self.extent = extent


class _StubSpatialReference(object):
    """arcpy.SpatialReference in the three attributes used here."""

    def __init__(self, wkid):
        self.factoryCode = wkid
        self.type = "Geographic" if wkid == 4326 else "Projected"
        self.metersPerUnit = None if wkid == 4326 else 0.3048006096012192


class _StubCursor(object):
    """arcpy.da.SearchCursor and UpdateCursor, in the surface used here."""

    def __init__(self, table, fields, sr, writable):
        self._table = table
        self._fields = list(fields)
        self._sr = sr
        self._writable = writable
        self._oid = None
        table.opened.append((list(fields), None if sr is None else sr.factoryCode,
                             writable))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _value(self, row, field):
        if field == "OID@":
            return row["OBJECTID"]
        if field in ("SHAPE@X", "SHAPE@Y"):
            geom = row["geom"]
            if geom is None:
                return None
            code = self._sr.factoryCode
            if code not in geom:
                raise RuntimeError("the stub holds no geometry in %s" % code)
            return geom[code][0 if field == "SHAPE@X" else 1]
        return row[field]

    def __iter__(self):
        for oid in sorted(self._table.rows):
            self._oid = oid
            row = self._table.rows[oid]
            yield [self._value(row, f) for f in self._fields]

    def updateRow(self, row):
        if not self._writable:
            raise RuntimeError("a SearchCursor cannot update")
        if self._table.fail_write:
            raise RuntimeError("the database refused the write")
        self._table.updates += 1
        for name, value in zip(self._fields, row):
            if name != "OID@":
                self._table.rows[self._oid][name] = value


class _StubEditor(object):
    """arcpy.da.Editor, recording the calls rather than performing them."""

    def __init__(self, table, workspace):
        self._table = table
        table.editor_calls.append("Editor(%s)" % workspace)

    def startEditing(self, with_undo, multiuser):
        self._table.editor_calls.append("startEditing")

    def startOperation(self):
        self._table.editor_calls.append("startOperation")

    def stopOperation(self):
        self._table.editor_calls.append("stopOperation")

    def abortOperation(self):
        self._table.editor_calls.append("abortOperation")
        self._table.rows = dict((oid, dict(row))
                                for oid, row in self._table.before.items())

    def stopEditing(self, save):
        self._table.editor_calls.append("stopEditing(%s)" % save)


class _StubDa(object):
    """The arcpy.da namespace."""

    def __init__(self, table):
        self._table = table

    def SearchCursor(self, layer, fields, spatial_reference=None):
        return _StubCursor(self._table, fields, spatial_reference, False)

    def UpdateCursor(self, layer, fields):
        return _StubCursor(self._table, fields, None, True)

    def Editor(self, workspace):
        return _StubEditor(self._table, workspace)


class _StubArcpy(object):
    """A point feature class in a dict, with the arcpy surface this tool touches.

    It exists so the read pass, the update cursor, the edit session and the
    failed-write rollback are exercised for real with no Esri software present,
    rather than only described in the README.
    """

    FC = "stub.gdb/Points"

    def __init__(self, rows, types=None):
        self.rows = dict((row["OBJECTID"], dict(row)) for row in rows)
        self.before = dict((oid, dict(row)) for oid, row in self.rows.items())
        self.types = dict(types or STUB_TYPES)
        self.updates = 0
        self.fail_write = False
        self.editor_calls = []
        # Every cursor opened, as (fields, wkid, writable). The apply path must
        # never open a writing cursor that carries a geometry token.
        self.opened = []
        # What Describe reports. A test that wants state plane magnitudes
        # replaces it rather than restating every row.
        self.extent = _StubExtent(-82.7, 29.2, -82.1, 29.8)
        self.da = _StubDa(self)

    def Exists(self, path):
        return path == self.FC

    def Describe(self, layer):
        return _StubDescribe(self.extent)

    def ListFields(self, layer):
        return [_StubField(n, t) for n, t in sorted(self.types.items())]

    def SpatialReference(self, wkid):
        return _StubSpatialReference(wkid)


STUB_TYPES = {"OBJECTID": "OID", "X": "Double", "Y": "Double",
              "LABEL": "String"}


def _stub_rows():
    """Seven rows: three clean, two drifted, one never filled, one with no shape."""
    def row(oid, sx, sy, gx, gy, geom=True):
        return {"OBJECTID": oid, "X": sx, "Y": sy, "LABEL": "p%d" % oid,
                "geom": None if not geom else {
                    4326: (gx, gy), 2237: (gx * 1000.0, gy * 1000.0)}}
    return [
        row(1, -82.1, 29.2, -82.1, 29.2),
        row(2, -82.2, 29.3, -82.2, 29.3),
        row(3, -82.3, 29.4, -82.3, 29.4),
        # OBJECTID 4 is the one that moved: about 11 m east.
        row(4, -82.4, 29.5, -82.3999, 29.5),
        # OBJECTID 5 was never populated.
        row(5, None, None, -82.5, 29.6),
        # OBJECTID 6 has no geometry and a stored coordinate worth keeping.
        row(6, -82.6, 29.7, 0.0, 0.0, geom=False),
        # OBJECTID 7 moved north only, so its X must not be rewritten.
        row(7, -82.7, 29.8, -82.7, 29.8001),
    ]


def _args(**kwargs):
    """Parsed arguments for a stub run, with the defaults filled in."""
    argv = ["--layer", _StubArcpy.FC]
    for key, value in sorted(kwargs.items()):
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value is not None:
            argv.extend([flag, str(value)])
    return _parse(argv)


def self_test():
    """Assertions over the decision core. No arcpy, no database, no network."""
    import contextlib
    import io

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def run_stub(stub, **kwargs):
        """run() against the stub, returning (exit code, printed text)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with contextlib.redirect_stderr(out):
                code = run(_args(**kwargs), stub)
        return code, out.getvalue()

    print("xydrift self-test: no arcpy, no database, no network")
    print("-" * 68)

    tol = DEFAULT_TOLERANCE_DEGREES

    # ---- measuring one axis
    check(delta(29.2, 29.2) == 0.0, "an identical coordinate is 0 apart")
    check(delta(29.2, 29.3) == delta(29.3, 29.2),
          "a plain distance is the same in both directions")
    check(wrapped_delta(-180.0, 180.0) == 0.0,
          "-180 and +180 are the same meridian, not 360 degrees of drift  <-- pinned defect")
    check(abs(wrapped_delta(179.9999995, -179.9999995) - 1e-6) < 1e-12,
          "two points either side of the antimeridian are 1e-6 degrees apart")
    check(wrapped_delta(-82.1, -82.1) == 0.0, "wrapping leaves an ordinary pair alone")
    check(wrapped_delta(0.0, 400.0) == 40.0,
          "a difference over 360 wraps to 40 and never comes back negative")
    check(wrapped_delta(0.0, 200.0) == 160.0, "a 200 degree gap measures 160 the short way")
    check(delta(0.0, 400.0) == 400.0, "without wrapping the same pair measures 400")

    # ---- the two null branches, which decide whether a row survives
    check(classify_pair(None, None, tol) == NO_GEOMETRY,
          "a row with neither a stored value nor geometry is not an error")
    check(classify_pair(-82.6, None, tol) == NO_GEOMETRY,
          "geometry of None leaves the stored coordinate alone  <-- pinned defect")
    check(classify_pair(None, -82.5, tol) == FILL,
          "a stored None with real geometry is filled in")
    check(classify_pair(-82.1, -82.1, tol) == OK, "an equal pair is not drift")
    check(classify_pair(0.0, 0.0, tol) == OK,
          "a coordinate of exactly zero is a value, not a missing one")

    # ---- the tolerance edge, which is a strict greater-than
    check(classify_pair(0.0, tol, tol) == OK,
          "a difference of exactly the tolerance is inside it  <-- pinned defect")
    check(classify_pair(0.0, math.nextafter(tol, 1.0), tol) == DRIFT,
          "one ulp above the tolerance is drift")
    check(classify_pair(0.0, math.nextafter(tol, 0.0), tol) == OK,
          "one ulp below the tolerance is not drift")
    check(classify_pair(0.0, -math.nextafter(tol, 1.0), tol) == DRIFT,
          "drift in the negative direction counts the same")
    check(classify_pair(-82.4, -82.3999, tol) == DRIFT,
          "a real 11 m move is drift")

    # ---- float noise against the rounding grid
    noisy = 0.1 + 0.2
    check(classify_pair(noisy, 0.3, tol) == OK,
          "0.1 plus 0.2 does not read as drift at the default tolerance")
    check(classify_pair(noisy, 0.3, 1e-18) == DRIFT,
          "the same pair reads as drift below the rounding grid, which is why "
          "the grid is a floor")
    manufactured = [v for v in (-82.123456789, 29.987654321, -180.0, 0.0,
                                179.999999994, -0.000000004)
                    if classify_pair(round(v, GEOM_DECIMALS), v, tol) != OK]
    check(manufactured == [],
          "rounding the geometry to 8 decimals never manufactures drift at 1e-6  <-- pinned defect")
    check(rounding_floor() == 1e-8, "the rounding grid is 1e-8 degrees")
    check(rounding_floor(2) == 0.01, "the grid follows the decimals it is given")

    # ---- measure reports the distance, not the verdict
    check(measure(None, 1.0) is None, "nothing is measured against a stored None")
    check(measure(1.0, None) is None, "nothing is measured against absent geometry")
    check(abs(measure(-82.4, -82.3999) - 1e-4) < 1e-12,
          "the measured drift is the distance between the two values")
    check(measure(-180.0, 180.0, True) == 0.0,
          "the measured drift across the antimeridian is zero, not 360")

    # ---- the row verdict
    check(worst(OK, DRIFT) == DRIFT, "a drifted column outranks a clean one")
    check(worst(NO_GEOMETRY, DRIFT) == NO_GEOMETRY,
          "a row with no geometry is reported as that, not as drift")
    check(worst(FILL, OK) == FILL, "a column to fill outranks a clean one")
    check(worst(OK, OK) == OK, "two clean columns make a clean row")
    raises(lambda: worst(), "an empty verdict list is refused")

    clean = plan_row(1, -82.1, 29.2, -82.1, 29.2, tol)
    check(clean.verdict == OK and not clean.writes,
          "a clean row plans no write at all")
    moved_x = plan_row(4, -82.4, 29.5, -82.3999, 29.5, tol)
    check(moved_x.verdict == DRIFT, "a row whose X moved is DRIFT")
    check(moved_x.new_x == -82.3999 and moved_x.new_y is None,
          "only the column that drifted is rewritten  <-- pinned defect")
    moved_y = plan_row(4, -82.4, 29.5, -82.4, 29.5001, tol)
    check(moved_y.new_y == 29.5001 and moved_y.new_x is None,
          "a drifted Y leaves X alone")
    empty = plan_row(5, None, None, -82.5, 29.6, tol)
    check(empty.verdict == FILL and empty.new_x == -82.5 and empty.new_y == 29.6,
          "a never-populated row is filled from its geometry")
    half = plan_row(5, None, 29.6, -82.5, 29.6, tol)
    check(half.verdict == FILL and half.new_y is None,
          "a half-populated row fills only the empty column")
    shapeless = plan_row(6, -82.6, 29.7, None, None, tol)
    check(shapeless.verdict == NO_GEOMETRY, "a row with no geometry says so")
    check(not shapeless.writes,
          "a row with no geometry keeps its stored coordinate  <-- pinned defect")
    check(shapeless.dx is None and shapeless.dy is None,
          "a row with no geometry reports no measured drift")
    check(abs(moved_x.dx - 1e-4) < 1e-12 and moved_x.dy == 0.0,
          "the plan carries the measured drift on both axes")
    check(repr(moved_x).startswith("RowPlan(4, DRIFT"),
          "a plan says which row and which verdict it is")

    anti = plan_row(7, -180.0, 29.0, 180.0, 29.0, tol, wrap=True)
    check(anti.verdict == OK,
          "a point on the antimeridian is not drift when wrapping is on")
    anti_flat = plan_row(7, -180.0, 29.0, 180.0, 29.0, tol, wrap=False)
    check(anti_flat.verdict == DRIFT,
          "the same row is drift with wrapping off, so the flag is live")

    # ---- tolerance and units
    check(resolve_tolerance(1e-6, "degrees", True) == 1e-6,
          "a degree tolerance on a geographic layer passes through")
    check(resolve_tolerance(1e-6, None, True) == 1e-6,
          "no units on a geographic layer means degrees")
    check(0.11 < DEFAULT_TOLERANCE_DEGREES * METERS_PER_DEGREE < 0.12,
          "the default tolerance is about 0.11 m")
    half_metre = resolve_tolerance(0.5, "meters", True)
    check(abs(half_metre - 0.5 / METERS_PER_DEGREE) < 1e-15,
          "half a metre against a lon/lat column converts to degrees  <-- pinned defect")
    check(half_metre < 1e-5,
          "the converted tolerance is small, not the 0.5 degrees a passthrough "
          "would have accepted")
    check(abs(resolve_tolerance(1.0, "feet", True)
              - METERS_PER_FOOT / METERS_PER_DEGREE) < 1e-15,
          "a foot tolerance converts through metres")
    check(resolve_tolerance(1.0, "meters", True) == 1.0 / METERS_PER_DEGREE,
          "the metre conversion uses the equatorial degree, so it never widens")
    check(resolve_tolerance(2.0, None, False) == 2.0,
          "no units on a projected layer means the layer's own units")
    check(resolve_tolerance(1.0, "meters", False, 1.0) == 1.0,
          "a metre tolerance on a metre layer is the same number")
    check(abs(resolve_tolerance(1.0, "meters", False, 0.3048006096012192)
              - 3.2808333) < 1e-5,
          "a metre tolerance on a US survey foot layer converts to 3.28 units")
    check(abs(resolve_tolerance(1.0, "feet", False, 0.3048006096012192)
              - 0.99999800) < 1e-6,
          "a foot tolerance on a US survey foot layer is very nearly one unit")
    raises(lambda: resolve_tolerance(1.0, "degrees", False),
           "a degree tolerance against a projected layer is refused  <-- pinned defect")
    raises(lambda: resolve_tolerance(1.0, "meters", False, None),
           "a metre tolerance is refused when the layer reports no metres per unit")
    raises(lambda: resolve_tolerance(1.0, "meters", False, 0),
           "a layer reporting zero metres per unit is refused, not divided by")
    raises(lambda: resolve_tolerance(0.0, "degrees", True),
           "a tolerance of zero is not a distance")
    raises(lambda: resolve_tolerance(-1.0, "degrees", True),
           "a negative tolerance is refused")
    raises(lambda: resolve_tolerance(None, "degrees", True),
           "a missing tolerance is refused")
    raises(lambda: resolve_tolerance(1e-9, "degrees", True),
           "a tolerance under the 1e-8 rounding grid is refused  <-- pinned defect")
    raises(lambda: resolve_tolerance(1e-8, "degrees", True),
           "a tolerance exactly at the grid is refused too")
    raises(lambda: resolve_tolerance(1e-9, None, False),
           "a projected tolerance under the grid is refused as well, because "
           "the geometry is rounded in feet too  <-- pinned defect")
    raises(lambda: resolve_tolerance(1e-9, "meters", False, 1.0),
           "a converted projected tolerance is checked against the grid after "
           "the conversion, not before")
    check(resolve_tolerance(1e-7, None, False) == 1e-7,
          "a projected tolerance above the grid still passes through")
    raises(lambda: resolve_tolerance(1.0, "furlongs", True),
           "an unknown unit on a geographic layer is refused")
    raises(lambda: resolve_tolerance(1.0, "furlongs", False, 1.0),
           "an unknown unit on a projected layer is refused")

    # ---- which columns may hold a coordinate
    check(refuse_coord_field("X", "Double", 1e-6, 180.0) is None,
          "a Double column is comparable")
    reason = refuse_coord_field("X", "String", 1e-6, 180.0)
    check(reason is not None and "String" in reason,
          "a text coordinate column is refused, not compared  <-- pinned defect")
    check("X" in reason, "the refusal names the column")
    check(refuse_coord_field("X", "Integer", 1e-6, 180.0) is not None,
          "an Integer column cannot hold a fractional coordinate")
    check(refuse_coord_field("X", "SmallInteger", 1e-6, 180.0) is not None,
          "a SmallInteger column is refused")
    check(refuse_coord_field("X", "Date", 1e-6, 180.0) is not None,
          "a Date column is refused")
    check(refuse_coord_field("X", "Single", 1e-6, 180.0) is not None,
          "a Single column near longitude 180 cannot hold 1e-6 degrees  <-- pinned defect")
    check(refuse_coord_field("X", "Single", 1e-3, 180.0) is None,
          "the same Single column is fine at a 1e-3 tolerance, so the check "
          "reads the numbers rather than banning a type")
    check(single_step(82.0) == 2.0 ** -17,
          "a Single near 82 steps by 2**-17, about 7.6e-6")
    check(single_step(1.0) == 2.0 ** -23, "a Single near 1 steps by 2**-23")
    check(single_step(0.0) == 0.0, "a Single at zero has no step to report")
    check(single_step(-82.0) == single_step(82.0),
          "the step does not depend on the sign")
    check(refuse_coord_field("X", "Single", single_step(180.0), 180.0) is None,
          "a Single whose step is exactly the tolerance is accepted, the same "
          "way the drift comparison treats its own edge")
    check(refuse_coord_field(
        "X", "Single", math.nextafter(single_step(180.0), 0.0), 180.0)
        is not None, "one ulp tighter than that step and the column is refused")
    # The projected half of the same check. A Single easting in state plane
    # feet is the case that bites, and nothing about the tolerance alone can
    # see it: the magnitude has to come from the layer.
    check(single_step(600000.0) == 0.0625,
          "a Single near 600000 feet steps by a sixteenth of a foot")
    check(refuse_coord_field("EAST", "Single", 0.01, 600000.0) is not None,
          "a Single easting cannot hold a hundredth of a foot at state plane "
          "magnitudes  <-- pinned defect")
    check(refuse_coord_field("EAST", "Single", 1.0, 600000.0) is None,
          "the same column is fine at a one foot tolerance")

    # ---- the report
    plans = [plan_row(i, -82.0, 29.0, -82.0, 29.0, tol) for i in range(1, 4)]
    plans.append(plan_row(4, -82.4, 29.5, -82.3999, 29.5, tol))
    plans.append(plan_row(5, None, None, -82.5, 29.6, tol))
    plans.append(plan_row(6, -82.6, 29.7, None, None, tol))
    counts = summarize(plans)
    check(counts[OK] == 3 and counts[DRIFT] == 1 and counts[FILL] == 1
          and counts[NO_GEOMETRY] == 1, "six rows count 3 OK, 1 DRIFT, 1 FILL, 1 NO_GEOMETRY")
    check(sorted(summarize([]).values()) == [0, 0, 0, 0],
          "an empty layer reports every verdict as zero")
    check(len(to_write(plans)) == 2,
          "only the drifted row and the empty row would be written")
    text = "\n".join(describe(plans, tol, "degrees"))
    check("rows read: 6" in text, "the report says how many rows it read")
    check("tolerance: 1e-06 degrees" in text,
          "the report states the tolerance it used and its units")
    check("OID 4 DRIFT" in text, "the report names the drifted row")
    check("dy=none" in text, "an unmeasurable axis prints as none, not as zero")
    check("1 row(s) have no geometry" in text,
          "the report says how many rows were left alone")
    check("... and" not in text, "six rows do not need a truncation line")
    many = [plan_row(i, -82.4, 29.5, -82.3999, 29.5, tol) for i in range(1, 15)]
    check("... and 4 more" in "\n".join(describe(many, tol, "degrees")),
          "past the limit the report says how many more there are")
    check("... and 13 more" in "\n".join(describe(many, tol, "degrees", 1)),
          "the limit is the number it was given")
    listed = [l for l in describe(many, tol, "degrees", 3) if l.startswith("  OID")]
    check(len(listed) == 3,
          "the limit is how many rows are listed, not only what the tail line "
          "claims  <-- pinned defect")
    clean_text = "\n".join(describe(plans[:3], tol, "degrees"))
    check("Every stored coordinate agrees" in clean_text,
          "a clean layer gets a sentence, not an empty list")
    check(_fmt(None) == "none" and _fmt(0.0001) == "0.0001",
          "a measured drift prints as a number and an absent one as none")

    # ---- the writing cursor
    check("SHAPE@X" not in update_fields("X", "Y")
          and "SHAPE@Y" not in update_fields("X", "Y"),
          "the update cursor carries no geometry token  <-- pinned defect")
    check(update_fields("PT_X", "PT_Y") == ["OID@", "PT_X", "PT_Y"],
          "the update cursor opens on the OID and the two named columns")

    # ---- reading a real cursor, with a stub geodatabase
    stub = _StubArcpy(_stub_rows())
    sr, geographic, mpu, types = layer_profile(stub, _StubArcpy.FC, 4326)
    check(geographic is True, "4326 profiles as a geographic layer")
    check(mpu is None, "a geographic layer reports no metres per unit")
    check(types["X"] == "Double" and types["LABEL"] == "String",
          "the profile reads the column types from the layer")
    projected = layer_profile(stub, _StubArcpy.FC, 2237)
    check(projected[1] is False and projected[2] > 0.3,
          "2237 profiles as projected and reports its metres per unit")

    # ---- the magnitude the Single check is made against
    check(layer_magnitude(stub, _StubArcpy.FC, True) == 180.0,
          "a geographic layer is bounded by the globe, so nothing is read")
    check(layer_magnitude(stub, _StubArcpy.FC, False) == 82.7,
          "a projected layer reports the largest corner of its own extent")
    wide = _StubArcpy(_stub_rows())
    wide.extent = _StubExtent(560000.0, 1740000.0, 640000.0, 1890000.0)
    check(layer_magnitude(wide, _StubArcpy.FC, False) == 1890000.0,
          "a state plane extent reports its largest coordinate, not its "
          "easting  <-- pinned defect")
    empty_extent = _StubArcpy(_stub_rows())
    empty_extent.extent = _StubExtent(None, None, None, None)
    check(layer_magnitude(empty_extent, _StubArcpy.FC, False) == 0.0,
          "an empty layer reports no extent and refuses no column for it")

    scanned = scan(stub, _StubArcpy.FC, "X", "Y", sr, tol, True)
    check(len(scanned) == 7, "the scan reads every row once")
    check(stub.opened[-1][1] == 4326,
          "the scan passes the requested spatial reference to the cursor")
    check(stub.opened[-1][2] is False, "the scan opens no writing cursor")
    verdicts = dict((p.oid, p.verdict) for p in scanned)
    check(verdicts == {1: OK, 2: OK, 3: OK, 4: DRIFT, 5: FILL, 6: NO_GEOMETRY,
                       7: DRIFT},
          "the scan finds the two moved rows, the empty row and the one "
          "without geometry")
    projected_scan = scan(stub, _StubArcpy.FC, "X", "Y",
                          stub.SpatialReference(2237), 1.0, False)
    check(projected_scan[0].verdict == DRIFT,
          "read in another coordinate system the same rows are all wrong, "
          "which is what makes the system part of the comparison")

    # The rounding inside the scan, exercised rather than described. The
    # geometry token returns the full float64, and its last bits are not the
    # number that was stored.
    noisy = _StubArcpy([{"OBJECTID": 1, "X": -82.1, "Y": 29.2, "LABEL": "n",
                         "geom": {4326: (-82.100000004, 29.200000004),
                                  2237: (0.0, 0.0)}}])
    fine = 1e-9
    check(scan(noisy, _StubArcpy.FC, "X", "Y", noisy.SpatialReference(4326),
               fine, True)[0].verdict == OK,
          "the scan rounds the geometry, so noise under the grid is not "
          "drift  <-- pinned defect")
    check(classify_pair(-82.1, -82.100000004, fine) == DRIFT,
          "the same pair compared unrounded is drift, so the rounding in the "
          "scan is what did the work")

    # ---- the apply path
    stub = _StubArcpy(_stub_rows())
    plans = scan(stub, _StubArcpy.FC, "X", "Y", stub.SpatialReference(4326),
                 tol, True)
    written = resync(stub, _StubArcpy.FC, plans, "X", "Y")
    check(written == 3, "the resync writes the three rows that needed it")
    check(stub.updates == 3, "no other row was handed to updateRow")
    check(stub.rows[4]["X"] == -82.3999, "the drifted X is now the geometry's X")
    check(stub.rows[4]["Y"] == 29.5, "the Y that already agreed is unchanged")
    check(stub.rows[7]["Y"] == 29.8001 and stub.rows[7]["X"] == -82.7,
          "a row that moved north has its Y rewritten and its X left alone  <-- pinned defect")
    check(stub.rows[5]["X"] == -82.5 and stub.rows[5]["Y"] == 29.6,
          "the empty row is filled from its geometry")
    check(stub.rows[6]["X"] == -82.6,
          "the row with no geometry keeps its stored coordinate  <-- pinned defect")
    check(stub.rows[1]["X"] == -82.1, "a clean row is not touched")
    check(stub.rows[4]["LABEL"] == "p4",
          "no column outside X and Y is read or written")
    writers = [fields for fields, _, writable in stub.opened if writable]
    check(writers and all("SHAPE@X" not in f and "SHAPE@Y" not in f
                          for f in writers),
          "no writing cursor was opened with a geometry token  <-- pinned defect")
    check(stub.editor_calls == [],
          "without a workspace no edit session is opened")
    check(resync(stub, _StubArcpy.FC, plans, "X", "Y") == 3,
          "a second resync of the same plan writes the same rows again, "
          "because the plan is the instruction and not the layer")
    clean_stub = _StubArcpy(_stub_rows()[:3])
    clean_plans = scan(clean_stub, _StubArcpy.FC, "X", "Y",
                       clean_stub.SpatialReference(4326), tol, True)
    check(resync(clean_stub, _StubArcpy.FC, clean_plans, "X", "Y") == 0,
          "a clean layer resyncs no rows")
    check([f for f, _, w in clean_stub.opened if w] == [],
          "and opens no update cursor at all, rather than opening one and "
          "finding nothing to do  <-- pinned defect")
    check(clean_stub.updates == 0, "and writes nothing")

    # ---- the edit session
    stub = _StubArcpy(_stub_rows())
    plans = scan(stub, _StubArcpy.FC, "X", "Y", stub.SpatialReference(4326),
                 tol, True)
    resync(stub, _StubArcpy.FC, plans, "X", "Y", workspace="stub.gdb")
    check(stub.editor_calls == ["Editor(stub.gdb)", "startEditing",
                                "startOperation", "stopOperation",
                                "stopEditing(True)"],
          "with a workspace the write is wrapped in an edit operation")

    stub = _StubArcpy(_stub_rows())
    plans = scan(stub, _StubArcpy.FC, "X", "Y", stub.SpatialReference(4326),
                 tol, True)
    stub.fail_write = True
    try:
        resync(stub, _StubArcpy.FC, plans, "X", "Y", workspace="stub.gdb")
    except RuntimeError:
        check(True, "a refused write is raised, not swallowed")
    else:
        check(False, "a refused write is raised, not swallowed")
    check("abortOperation" in stub.editor_calls,
          "a refused write aborts the edit operation")
    check(stub.editor_calls[-1] == "stopEditing(False)",
          "and closes the session without saving")
    check(stub.rows[4]["X"] == -82.4,
          "after the abort the layer holds what it held before  <-- pinned defect")

    stub = _StubArcpy(_stub_rows())
    plans = scan(stub, _StubArcpy.FC, "X", "Y", stub.SpatialReference(4326),
                 tol, True)
    stub.fail_write = True
    try:
        resync(stub, _StubArcpy.FC, plans, "X", "Y")
    except RuntimeError:
        check(True, "a refused write without an edit session is raised too")
    else:
        check(False, "a refused write without an edit session is raised too")

    # ---- end to end through run()
    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub)
    check(code == 1, "drift found and not written exits 1")
    check(stub.updates == 0,
          "a check run writes nothing at all  <-- pinned defect")
    check("Re-run with --apply to resync 3 row(s)" in out,
          "the check run says how many rows an apply would touch")
    check("tolerance: 1e-06 degrees" in out,
          "the default tolerance on a geographic layer is 1e-6 degrees")

    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, apply=True, workspace="stub.gdb")
    check(code == 0, "a successful resync exits 0")
    check("resynced 3 row(s)" in out, "and says how many rows it wrote")
    check(stub.rows[4]["X"] == -82.3999, "and the row is now correct")

    clean_stub = _StubArcpy(_stub_rows()[:3])
    code, out = run_stub(clean_stub)
    check(code == 0, "a layer with no drift exits 0")
    check("Every stored coordinate agrees" in out, "and says so")

    stub = _StubArcpy(_stub_rows())
    stub.fail_write = True
    code, out = run_stub(stub, apply=True)
    check(code == 2, "a resync that fails part way exits 2")
    check("FAILED" in out, "and prints the reason")

    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, tolerance=1.0, tolerance_units="meters")
    check(code == 1 and "tolerance: 8.98" in out,
          "a one metre tolerance is reported in degrees, not in metres")
    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, tolerance=100.0, tolerance_units="meters")
    check("  DRIFT             0" in out,
          "a 100 m tolerance hides the 11 m move, which is the other way a "
          "tolerance ruins this  <-- pinned defect")
    check(code == 1 and "resync 1 row(s)" in out,
          "the never-populated row is still reported, because no tolerance "
          "makes an empty column agree with a geometry")

    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, limit=1)
    check("... and 2 more" in out, "--limit reaches the report")

    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, wkid=2237)
    check(code == 64 and "no sane default tolerance" in out,
          "a projected layer refuses to invent a tolerance  <-- pinned defect")
    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, wkid=2237, tolerance=1.0, tolerance_units="degrees")
    check(code == 64 and "means nothing against a projected layer" in out,
          "a degree tolerance against a projected layer is refused by name")
    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, wkid=2237, tolerance=1.0)
    check(code == 1 and "linear units" in out,
          "a projected layer with an explicit tolerance runs and reports "
          "linear units")

    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, x_field="LON")
    check(code == 64 and "is not in" in out, "a column that is not there is refused")
    # A Single easting column in state plane feet, checked to a hundredth of a
    # foot. The column's own step is 0.0625 ft, so every row would read as
    # drifted for ever, and no tolerance the caller can pick changes that.
    single = _StubArcpy(_stub_rows(),
                        types={"OBJECTID": "OID", "X": "Single",
                               "Y": "Single", "LABEL": "String"})
    single.extent = _StubExtent(560000.0, 1740000.0, 640000.0, 1890000.0)
    code, out = run_stub(single, wkid=2237, tolerance=0.01)
    check(code == 64 and "coarser than the tolerance" in out,
          "a Single column on a projected layer is refused from the layer's "
          "own extent  <-- pinned defect")
    check(single.opened == [],
          "and that refusal also happens before any cursor opens")
    single = _StubArcpy(_stub_rows(),
                        types={"OBJECTID": "OID", "X": "Single",
                               "Y": "Single", "LABEL": "String"})
    single.extent = _StubExtent(560000.0, 1740000.0, 640000.0, 1890000.0)
    code, out = run_stub(single, wkid=2237, tolerance=1.0)
    check(code == 1, "the same layer at a one foot tolerance runs")

    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, x_field="LABEL")
    check(code == 64 and "compared as a number" in out,
          "a String column is refused before any cursor opens")
    check(stub.opened == [],
          "and the refusal happens before the layer is read  <-- pinned defect")

    missing = _StubArcpy(_stub_rows())
    out = io.StringIO()
    with contextlib.redirect_stderr(out):
        code = run(_parse(["--layer", "nowhere.gdb/Points"]), missing)
    check(code == 64 and "does not exist" in out.getvalue(),
          "a layer that does not exist is refused")

    # ---- argument handling
    a = _parse(["--layer", "L"])
    check(a.apply is False, "--apply defaults to OFF")
    check(a.tolerance is None, "--tolerance has no default here, the layer decides")
    check(a.tolerance_units is None, "--tolerance-units defaults to the layer's own")
    check(a.x_field == "X" and a.y_field == "Y", "the columns default to X and Y")
    check(a.wkid == DEFAULT_WKID, "--wkid defaults to 4326")
    check(a.limit == DEFAULT_LIMIT, "--limit defaults to 10")
    check(a.workspace is None, "--workspace defaults to none")
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    check(_parse(["--layer", "L", "--tolerance", "0.5"]).tolerance == 0.5,
          "--tolerance is read as a number")
    check(_parse(["--layer", "L", "--tolerance-units",
                  "feet"]).tolerance_units == "feet", "--tolerance-units is read")
    check(_parse(["--layer", "L", "--x-field", "PT_X",
                  "--y-field", "PT_Y"]).x_field == "PT_X",
          "--x-field is read")
    check(_parse(["--layer", "L", "--wkid", "2237"]).wkid == 2237,
          "--wkid is read")
    check(_parse(["--layer", "L", "--limit", "3"]).limit == 3, "--limit is read")
    check(_parse(["--layer", "L", "--workspace", "w"]).workspace == "w",
          "--workspace is read")
    check(_parse(["--layer", "L", "--apply"]).apply is True, "--apply is read")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        bad_limit = main(["--layer", "L", "--limit", "-1"])
        no_layer = main([])
    check(bad_limit == 64, "a negative --limit is a usage error")
    check(no_layer == 64, "no layer and no --self-test is a usage error")
    check("--layer is required" in err.getvalue(),
          "and the usage error says which flag is missing")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="xydrift.py",
        description="Name the rows whose stored X and Y columns disagree with "
                    "their own geometry, and resync only those.",
        epilog="Nothing is written without --apply.",
    )
    ap.add_argument("--layer", help="point feature class to check")
    ap.add_argument("--x-field", dest="x_field", default="X",
                    help="stored longitude or easting column (default X)")
    ap.add_argument("--y-field", dest="y_field", default="Y",
                    help="stored latitude or northing column (default Y)")
    ap.add_argument("--tolerance", type=float,
                    help="largest difference that is not drift. Required for a "
                         "projected layer; a geographic layer defaults to %g "
                         "degrees, about 0.11 m."
                         % DEFAULT_TOLERANCE_DEGREES)
    ap.add_argument("--tolerance-units", dest="tolerance_units",
                    choices=("degrees", "meters", "feet"),
                    help="units the tolerance is given in. The default is the "
                         "comparison system's own units.")
    ap.add_argument("--wkid", type=int, default=DEFAULT_WKID,
                    help="coordinate system the stored columns are in "
                         "(default %d, WGS84 lon/lat)" % DEFAULT_WKID)
    ap.add_argument("--workspace",
                    help="geodatabase to open an edit session on. Needed for a "
                         "versioned feature class.")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="rows listed before the report counts the rest "
                         "(default %d)" % DEFAULT_LIMIT)
    ap.add_argument("--apply", action="store_true",
                    help="write the resynced coordinates. Without this nothing "
                         "is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.layer:
        print("error: --layer is required. Use --self-test to verify the tool "
              "without a geodatabase.", file=sys.stderr)
        return 64
    if args.limit < 0:
        print("error: --limit cannot be negative.", file=sys.stderr)
        return 64

    return run(args, _import_arcpy())


if __name__ == "__main__":
    sys.exit(main())
