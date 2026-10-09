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
    python xydrift.py --from-geojson points.geojson --xy-crs 4326
    python xydrift.py --layer prod.sde/Addresses --infer-crs
    python xydrift.py --layer prod.sde/Addresses --infer-crs 4326,2237,2236
    python xydrift.py --from-geojson points.geojson --infer-crs

--layer needs arcpy. --from-geojson and --self-test need only the standard library,
and --from-geojson never writes anything. --infer-crs never writes either: it
names the system the stored columns are in, or refuses on a tie or a mixed
layer.

Exit codes: 0 no drift or resync done, 1 drift found and not written, 2 the resync
failed part way, 3 arcpy is missing or failed while reading, and nothing was
written, 64 usage error, refused input, or no row had a geometry to compare.
--infer-crs exits 0 when it names one system and 64 when it refuses.
"""

from __future__ import print_function

import argparse
import json
import math
import os
import sys
import time

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

# The only system GeoJSON geometry is in. RFC 7946 fixes it at WGS84 lon/lat.
GEOJSON_WKID = 4326

# Names an old-style GeoJSON "crs" member may carry and still mean lon/lat.
# RFC 7946 removed the member; GDAL still writes the first name. Compared
# without regard to case.
LONLAT_CRS_NAMES = (
    "urn:ogc:def:crs:OGC:1.3:CRS84",
    "urn:ogc:def:crs:OGC::CRS84",
    "OGC:CRS84",
    "urn:ogc:def:crs:EPSG::4326",
    "EPSG:4326",
)

# --infer-crs. Web mercator (EPSG:3857) is tried by default beside the layer's
# own system and 4326, because hosted layers often store X and Y in it.
WEB_MERCATOR_WKID = 3857

# WGS84 semi-major axis, which EPSG:3857 uses as the radius of a sphere.
WEB_MERCATOR_RADIUS = 6378137.0

# The only systems a GeoJSON file can be tested in, because the standard
# library reprojects nothing and 3857 is closed form from lon/lat.
FILE_CANDIDATES = (GEOJSON_WKID, WEB_MERCATOR_WKID)

# Tolerance --infer-crs uses when none is given, in metres. It has to mean the
# same distance in every candidate, so it is never in degrees. Half a metre is
# above a column rounded to 5 decimals of a degree, and below the metre or so
# between NAD83 and WGS84, which a wider tolerance reports as a tie.
DEFAULT_INFER_TOLERANCE_METERS = 0.5

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


def wrapped_delta(a, b, slack=0.0):
    """Distance in degrees between two longitudes, the short way round.

    -180 and +180 are the same meridian. A plain subtraction calls them 360
    degrees apart, which reads as the largest drift the layer can hold on the
    one line where nothing moved at all.

    Only that seam is wrapped. A value past +/-180 is not a longitude, so it
    is measured plainly: a stored -442.2, or 277.8 in the 0-360 convention,
    is a whole turn from -82.2 and is drift, not a match.

    slack is the tolerance. A value that far past 180 or less is within the
    tolerance of the meridian, so it is still a longitude. Without it, a
    stored 180.0000001 was OK against +180 and 360 degrees of drift against
    -180, the same place, and --apply rewrote a row that agreed.
    """
    limit = 180.0 + slack
    if abs(rounded(a)) > limit or abs(rounded(b)) > limit:
        return abs(a - b)
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
    d = wrapped_delta(stored, new, tolerance) if wrap else delta(stored, new)
    # Strictly greater, as the source this was taken from has it. A value
    # exactly at the tolerance is inside the tolerance. Written as "inside,
    # else DRIFT" and not "greater, else OK": a NaN column gives a NaN
    # distance, which is never greater than a tolerance, and read as clean.
    return OK if d <= tolerance else DRIFT


def measure(stored, new, wrap=False, slack=0.0):
    """Distance between a stored coordinate and its geometry, or None."""
    if stored is None or new is None:
        return None
    return wrapped_delta(stored, new, slack) if wrap else delta(stored, new)


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
                   measure(stored_x, geom_x, wrap, tolerance),
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
    # NaN and infinity pass both "<= 0" and the grid check below, and then
    # no difference is ever greater than them: every drifted row reads OK.
    if not is_number(value) or value <= 0:
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


def nothing_compared(plans):
    """True when there were rows and not one of them had a geometry.

    Such a run compared nothing, so it cannot call the layer clean. A file
    exported without its geometry would otherwise pass a nightly check for
    ever.
    """
    return bool(plans) and all(p.verdict == NO_GEOMETRY for p in plans)


def describe(plans, tolerance, units_label, limit=DEFAULT_LIMIT):
    """The report, as lines. No printing here so the self-test can read it."""
    counts = summarize(plans)
    lines = ["rows read: %d" % len(plans),
             "tolerance: %g %s" % (tolerance, units_label)]
    for verdict in VERDICT_ORDER:
        lines.append("  %-12s %6d" % (verdict, counts[verdict]))

    moved = [p for p in plans if p.verdict in (DRIFT, FILL)]
    lines.append("")
    if moved:
        lines.append("rows whose columns disagree with their geometry:")
        for plan in moved[:limit]:
            lines.append("  OID %s %s: dx=%s dy=%s" % (
                printable(plan.oid), plan.verdict, _fmt(plan.dx),
                _fmt(plan.dy)))
        if len(moved) > limit:
            lines.append("  ... and %d more" % (len(moved) - limit))
    elif nothing_compared(plans):
        lines.append("No row has a geometry, so no stored coordinate was "
                     "compared.")
    elif counts[NO_GEOMETRY]:
        lines.append("Every stored coordinate that has a geometry agrees "
                     "with it.")
    else:
        lines.append("Every stored coordinate agrees with its geometry.")

    # After every branch, not only the drift one. Behind an early return it
    # printed only when drift was found as well.
    if counts[NO_GEOMETRY]:
        lines.append("")
        lines.append("%d row(s) have no geometry. Their stored coordinates are "
                     "left alone." % counts[NO_GEOMETRY])
    return lines


def _fmt(value):
    return "none" if value is None else "%g" % value


def printable(value):
    """Text of any value in printable ASCII, with the rest backslash-escaped.

    A feature id or a file name can hold a character the console cannot
    encode, and a lone surrogate that no encoding can. Printing either raw
    stops the report part way with a traceback that exits 1, the code for
    drift found. Control characters are ASCII and are escaped as well: a
    newline in an id forges a report line, and ESC[8m hides the real one.
    """
    text = ("%s" % (value,)).encode("ascii", "backslashreplace").decode("ascii")
    return "".join(c if " " <= c <= "~" else "\\x%02x" % ord(c)
                   for c in text)


def update_fields(x_field, y_field):
    """Fields the update cursor opens with.

    No geometry token appears here, and that is deliberate. An update cursor
    that carries SHAPE@ can write the shape back, and a rounded geometry token
    written back to a projected feature class moves the point. The geometry is
    read by a separate cursor and never travels on the writing one.
    """
    return ["OID@", x_field, y_field]


def rounded(value):
    """Geometry coordinate on the comparison grid, or None.

    Round before comparing, not after. A geometry arrives as a full float64,
    whose last bits differ from the value that was stored, and comparing those
    bits is how a tolerance ends up being asked to hide arithmetic.
    """
    return None if value is None else round(value, GEOM_DECIMALS)


def geometry_xy(gx, gy):
    """The geometry pair on the grid, or (None, None) if it is not a point.

    A pair with a NaN, an infinity or a missing axis has no position. It says
    nothing about the stored columns, so it plans as NO_GEOMETRY and nothing
    is written from it. Planned as numbers, a NaN geometry read as clean.
    """
    if gx is None or gy is None or not (math.isfinite(gx)
                                        and math.isfinite(gy)):
        return None, None
    return rounded(gx), rounded(gy)


def plan_rows(rows, tolerance, wrap):
    """Plan each (oid, stored x, stored y, geometry x, geometry y) row.

    The layer scan and the GeoJSON reader both end here, so the two inputs
    cannot disagree about what counts as drift.
    """
    plans = []
    for oid, sx, sy, gx, gy in rows:
        gx, gy = geometry_xy(gx, gy)
        plans.append(plan_row(oid, sx, sy, gx, gy, tolerance, wrap))
    return plans


def is_number(value):
    """True for a finite JSON number. A JSON true or false is not one.

    NaN is the case that bites. NaN minus anything is NaN, NaN is never
    greater than a tolerance, and classify_pair therefore calls a NaN column
    clean on every row.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # An integer too large for a float.
        return False


def xy_crs_refusal(crs):
    """Reason the stated system of the stored columns cannot be used, or None.

    GeoJSON geometry is always lon/lat, and the standard library cannot
    reproject. Columns in any other system would be compared in different
    units on every row, which reports all of them as drift, correctly and
    uselessly.
    """
    if crs is None:
        return ("--from-geojson needs --xy-crs, the EPSG code the stored X "
                "and Y columns are in. GeoJSON geometry is always WGS84 "
                "lon/lat (EPSG:%d), so the columns can be compared with it "
                "only if they are lon/lat too." % GEOJSON_WKID)
    if crs != GEOJSON_WKID:
        # NAD83 and NAD27 are lon/lat too, and are still refused. NAD27
        # against WGS84 is tens of metres, which is the drift being hunted.
        return ("--xy-crs %d: GeoJSON geometry is always WGS84 lon/lat "
                "(EPSG:%d), and columns in any other system, another lon/lat "
                "datum included, would be compared in the wrong frame. This "
                "mode cannot reproject, so it will not compare them. Export "
                "the columns in EPSG:%d, or run --layer with --wkid %d under "
                "ArcGIS Pro." % (crs, GEOJSON_WKID, GEOJSON_WKID, crs))
    return None


def geojson_crs_refusal(doc, owner="the file"):
    """Reason the geometry under doc is not lon/lat, or None.

    RFC 7946 has no crs member. An older file may still carry one, and a
    projected name there means the coordinates are feet or metres, whatever
    the standard says they should be. The 2008 spec allowed it on a feature
    and on a geometry as well, overriding the collection's, so feature_rows
    asks this of both. owner names where the member was found.
    """
    crs = doc.get("crs") if isinstance(doc, dict) else None
    if crs is None:
        return None
    props = crs.get("properties") if isinstance(crs, dict) else None
    name = props.get("name") if isinstance(props, dict) else None
    known = [n.upper() for n in LONLAT_CRS_NAMES]
    if isinstance(name, str) and name.strip().upper() in known:
        return None
    return ("%s declares the crs %s. GeoJSON geometry has to "
            "be WGS84 lon/lat to be compared here, and this mode cannot "
            "reproject." % (owner, json.dumps(crs, sort_keys=True)))


def point_xy(geometry, oid):
    """Longitude and latitude of one GeoJSON point, or (None, None).

    An absent geometry and an empty point both give (None, None), which
    plans as NO_GEOMETRY and leaves the stored columns alone. Anything else
    that is not a lon/lat point raises ValueError with the reason.
    """
    if geometry is None:
        return None, None
    kind = geometry.get("type") if isinstance(geometry, dict) else None
    if kind != "Point":
        raise ValueError("feature %s has a %s geometry. Only a point has one "
                         "X and one Y to compare."
                         % (printable(oid), printable(kind)))
    coords = geometry.get("coordinates")
    if coords == []:
        return None, None
    if (not isinstance(coords, list) or len(coords) < 2
            or not (is_number(coords[0]) and is_number(coords[1]))):
        raise ValueError("feature %s has point coordinates %s, which are not "
                         "a position."
                         % (printable(oid), json.dumps(coords)))
    x, y = float(coords[0]), float(coords[1])
    # Checked on the grid, so a writer's 180.00000000001 is still 180.
    if abs(rounded(x)) > 180.0 or abs(rounded(y)) > 90.0:
        raise ValueError("feature %s is at %r, %r, which is not lon/lat. The "
                         "file was written in another system, and this mode "
                         "cannot reproject." % (printable(oid), x, y))
    return x, y


def feature_rows(doc, x_field, y_field):
    """Rows for plan_rows from a parsed GeoJSON document.

    Each row is (oid, stored x, stored y, geometry x, geometry y). The OID is
    the feature's id member, or its 1-based position when it has none. A
    property that is absent reads as None, the same as a null. A column that
    no feature carries at all is refused, because that is a misspelled name
    and not a layer that was never populated.
    """
    kind = doc.get("type") if isinstance(doc, dict) else None
    if kind == "Feature":
        features = [doc]
    elif kind == "FeatureCollection":
        features = doc.get("features")
    else:
        raise ValueError("the file is not a GeoJSON FeatureCollection or "
                         "Feature.")
    if not isinstance(features, list):
        raise ValueError("the FeatureCollection has no features list.")

    if not features:
        # A layer has a schema to check the names against. An empty file has
        # none, so a misspelled column would pass, and a failed export would
        # report as clean.
        raise ValueError("the file has no features, so nothing can be "
                         "checked and the columns %s and %s cannot be "
                         "confirmed." % (printable(x_field), printable(y_field)))
    rows = []
    seen = set()
    for index, feature in enumerate(features, 1):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("item %d in features is not a Feature." % index)
        oid = feature.get("id", index)
        props = feature.get("properties")
        props = {} if props is None else props
        if not isinstance(props, dict):
            raise ValueError("feature %s has properties that are not an "
                             "object." % printable(oid))
        # A crs one level down was ignored, and NAD27 geometry compared with
        # WGS84 columns read as clean.
        for owner, obj in (("feature %s" % printable(oid), feature),
                           ("the geometry of feature %s" % printable(oid),
                            feature.get("geometry"))):
            reason = geojson_crs_refusal(obj, owner)
            if reason is not None:
                raise ValueError(reason)
        stored = []
        for field in (x_field, y_field):
            if field in props:
                seen.add(field)
            value = props.get(field)
            if value is not None and not is_number(value):
                raise ValueError("column %s holds %s on feature %s. A "
                                 "coordinate has to be compared as a finite "
                                 "number, and this one is not stored as one."
                                 % (field, json.dumps(value),
                                    printable(oid)))
            stored.append(None if value is None else float(value))
        gx, gy = point_xy(feature.get("geometry"), oid)
        rows.append((oid, stored[0], stored[1], gx, gy))

    for field in (x_field, y_field):
        if field not in seen:
            raise ValueError("column %s is not in the properties of any "
                             "feature." % field)
    return rows


def lonlat_refusal(rows, x_field, y_field):
    """Reason the stored columns are plainly not lon/lat, or None.

    --xy-crs is the caller's word. When not one stored value in a column
    could be a longitude or a latitude, the word was wrong: the columns are
    in feet or metres and every row would read as drift. One wild value among
    good ones is only a drifted row, and is reported as that.
    """
    for field, pos, limit in ((x_field, 1, 180.0), (y_field, 2, 90.0)):
        values = [r[pos] for r in rows if r[pos] is not None]
        if values and not any(abs(v) <= limit for v in values):
            return ("column %s holds no value within +/-%g, so it is not in "
                    "degrees, whatever --xy-crs says. Its first value is %r."
                    % (field, limit, values[0]))
    return None


def geojson_rows(doc, x_field, y_field):
    """Every check on a parsed file, then its rows. Raises ValueError."""
    reason = geojson_crs_refusal(doc)
    if reason is None:
        rows = feature_rows(doc, x_field, y_field)
        reason = lonlat_refusal(rows, x_field, y_field)
    if reason is not None:
        raise ValueError(reason)
    return rows


# ------------------------------------------------- --infer-crs, pure core

def web_mercator(lon, lat):
    """EPSG:3857 easting and northing of a WGS84 lon/lat, or (None, None).

    IOGP Guidance Note 7-2, method 1024 (Popular Visualisation Pseudo
    Mercator): E = R * lon, N = R * ln(tan(pi/4 + lat/2)), with R the WGS84
    semi-major axis. It is closed form, so a file can be tested against 3857
    with no projection library. A pole has no northing, so it has no position.
    """
    if lat is None or abs(lat) >= 90.0:
        return None, None
    return (WEB_MERCATOR_RADIUS * math.radians(lon),
            WEB_MERCATOR_RADIUS * math.log(
                math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)))


def candidate_agreement(rows, tolerance, wrap):
    """(rows read, keys compared, keys that agree) for one candidate system.

    rows are (key, stored x, stored y, geometry x, geometry y), with the
    geometry already in the candidate. A row is compared only when it has a
    geometry and both columns hold a finite number. An empty or NaN column is
    evidence for no system, and counted as compared it would count against
    every candidate alike.
    """
    read = 0
    compared = set()
    agree = set()
    for key, sx, sy, gx, gy in rows:
        read += 1
        gx, gy = geometry_xy(gx, gy)
        if gx is None or not (is_number(sx) and is_number(sy)):
            continue
        compared.add(key)
        if (classify_pair(sx, gx, tolerance, wrap) == OK
                and classify_pair(sy, gy, tolerance) == OK):
            agree.add(key)
    return read, compared, agree


def _epsg(codes):
    return ", ".join("EPSG %d" % code for code in codes)


def infer(results):
    """Rank the candidates and name one system, or say why none can be named.

    results is [(wkid, keys compared, keys that agree)] in the order the
    candidates were given. A row that agrees with exactly one candidate is
    evidence for it. A row that agrees with two is evidence for neither: a
    point at 0, 0 is the same numbers in degrees and in web mercator metres.

    The refusals, in order. Nothing compared. A tie, when most compared rows
    agree with two candidates alike, so the tolerance cannot separate them.
    A mixed layer, when two candidates each explain rows that no other does,
    so no one --wkid compares the layer. No candidate explains any row on
    its own. And a minority winner, when the one candidate left explains
    half the compared rows or fewer, because the rest agree with nothing.
    """
    order = [wkid for wkid, _, _ in results]
    agree = dict((wkid, keys) for wkid, _, keys in results)
    compared = set()
    for _, keys, _ in results:
        compared |= keys
    owners = {}
    for wkid in order:
        for key in agree[wkid]:
            owners.setdefault(key, []).append(wkid)
    exclusive = dict((wkid, set()) for wkid in order)
    shared = set()
    for key, wkids in owners.items():
        if len(wkids) == 1:
            exclusive[wkids[0]].add(key)
        else:
            shared.add(key)
    # Stable, so equal candidates keep the order they were given in.
    ranking = sorted(order, key=lambda w: (-len(exclusive[w]), -len(agree[w])))
    explaining = [wkid for wkid in ranking if exclusive[wkid]]

    winner = None
    if not compared:
        kind = "nothing"
        reason = ("no row has a geometry and a number in both columns, so "
                  "nothing was compared.")
    elif len(shared) * 2 > len(compared):
        kind = "tie"
        reason = ("a tie. %d of %d compared rows agree with %s alike. Those "
                  "systems are closer together here than the tolerance, so "
                  "the columns cannot tell them apart. Tighten --tolerance."
                  % (len(shared), len(compared),
                     _epsg(w for w in ranking if agree[w] & shared)))
    elif len(explaining) > 1:
        kind = "mixed"
        reason = ("a mixed layer. %s. One layer holds its columns in more "
                  "than one system, so no single --wkid can check it."
                  % "; ".join("%d row(s) agree only with EPSG %d"
                              % (len(exclusive[w]), w) for w in explaining))
    elif not explaining:
        kind = "none"
        reason = ("no candidate agrees with any compared row on its own. The "
                  "columns are in a system that is not on the list, or every "
                  "row has drifted.")
    elif len(agree[explaining[0]]) * 2 <= len(compared):
        kind = "minority"
        reason = ("only %d of %d compared rows agree with EPSG %d, and the "
                  "rest agree with no candidate. The system is not on the "
                  "list, or most rows have drifted."
                  % (len(agree[explaining[0]]), len(compared), explaining[0]))
    else:
        kind = "winner"
        reason = None
        winner = explaining[0]
    return {"ranking": ranking, "agree": agree, "compared": compared,
            "exclusive": exclusive, "shared": shared, "kind": kind,
            "reason": reason, "winner": winner}


def describe_inference(found, rows_read, tolerance_text, notes=None,
                       labels=None, limit=DEFAULT_LIMIT):
    """The --infer-crs report, as lines. Printing is the caller's."""
    notes = notes or {}
    labels = labels or {}
    lines = ["rows read: %d" % rows_read,
             "rows compared: %d (a geometry, and a number in both columns)"
             % len(found["compared"]),
             "tolerance: %s, converted into each candidate's own units"
             % tolerance_text,
             "",
             "candidates, ranked by the rows that only they explain:"]
    for wkid in found["ranking"]:
        line = "  EPSG %-6d agree %6d   only this one %6d" % (
            wkid, len(found["agree"][wkid]), len(found["exclusive"][wkid]))
        if wkid in notes:
            line += "   (%s)" % notes[wkid]
        lines.append(line)
    lines.append("rows that agree with more than one candidate: %d"
                 % len(found["shared"]))
    if found["kind"] == "mixed":
        # The rows are the actionable part: they came from somewhere else.
        for wkid in found["ranking"]:
            keys = sorted(found["exclusive"][wkid])
            if not keys:
                continue
            shown = ", ".join(printable(labels.get(k, k)) for k in keys[:limit])
            more = len(keys) - limit
            lines.append("rows that agree only with EPSG %d: OID %s%s" % (
                wkid, shown, " ... and %d more" % more if more > 0 else ""))
    return lines


# ------------------------------------------------------------------ files

def unique_names(pairs):
    """One JSON object as a dict, refusing a name that appears twice.

    Python keeps the last of two equal names without a word, and another
    reader may keep the first. {"X": -82.4, "X": -82.3999} would then be
    checked as whichever one happened to win.
    """
    obj = {}
    for name, value in pairs:
        if name in obj:
            raise ValueError("the name %s appears twice in one object, so the "
                             "file can be read two ways." % json.dumps(name))
        obj[name] = value
    return obj


def load_geojson(path):
    """Parse a GeoJSON file. Returns (document, modified time as UTC text).

    ponytail: the whole file is parsed into memory, like the plans. Fine for
    a few hundred thousand points; stream it if a file outgrows that.
    """
    with open(path, "rb") as handle:
        text = handle.read().decode("utf-8-sig")
    # Python's json accepts NaN and Infinity, which JSON does not. They are
    # left in, and is_number refuses them where they matter: in the two
    # columns and the geometry, and nowhere else in the file.
    doc = json.loads(text, object_pairs_hook=unique_names)
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC",
                              time.gmtime(os.path.getmtime(path)))
    except (OSError, OverflowError, ValueError):
        # Windows cannot convert a time before 1970, a 32-bit time_t cannot
        # hold a far-future one, and some platforms raise ValueError for
        # either. The time is only a label, so the check still runs rather
        # than exiting 1, the code for drift found.
        stamp = "unknown"
    return doc, stamp


def same_field_refusal(args):
    """Reason --x-field and --y-field cannot be used, or None.

    One column read as both axes is compared with the longitude and the
    latitude in turn, and --apply would write the latitude over it.
    """
    if args.x_field == args.y_field:
        return ("--x-field and --y-field both name %s. They have to be two "
                "columns." % printable(args.x_field))
    return None


def run_geojson(args):
    """The whole --from-geojson run. Needs no arcpy and never writes."""
    for flag, given, why in (
            ("--apply", args.apply,
             "A file is read, never written. A resync writes the layer "
             "itself: run --layer with --apply under ArcGIS Pro."),
            ("--workspace", args.workspace,
             "There is no edit session on a file."),
            ("--wkid", args.wkid != DEFAULT_WKID,
             "State the system of the stored columns with --xy-crs.")):
        if given:
            print("error: %s does not apply to --from-geojson. %s"
                  % (flag, why), file=sys.stderr)
            return 64

    reason = xy_crs_refusal(args.xy_crs) or same_field_refusal(args)
    if reason is not None:
        print("error: %s" % reason, file=sys.stderr)
        return 64

    tolerance = args.tolerance
    units = args.tolerance_units
    if tolerance is None:
        tolerance = DEFAULT_TOLERANCE_DEGREES
        units = "degrees"
    try:
        tolerance = resolve_tolerance(tolerance, units, True)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    try:
        doc, stamp = load_geojson(args.from_geojson)
        rows = geojson_rows(doc, args.x_field, args.y_field)
    except (OSError, ValueError, RecursionError, MemoryError) as exc:
        # RecursionError is a file nested too deep for the parser, and is
        # not a ValueError. Uncaught, it would exit 1: drift found.
        print("error: %s: %s" % (args.from_geojson, exc), file=sys.stderr)
        return 64

    plans = plan_rows(rows, tolerance, True)
    # The time is the local file's, and a copy resets it unless the copy
    # keeps it (scp -p, cp -p). The line says so, so nobody reads a year-old
    # export, copied this morning, as fresh.
    print("source: %s, file modified %s. That is the export time only if "
          "every copy kept it. A file is a snapshot, not the live layer."
          % (printable(args.from_geojson), stamp))
    for line in describe(plans, tolerance, "degrees", args.limit):
        print(line)
    if nothing_compared(plans):
        print("error: no feature has a geometry, so nothing was checked.",
              file=sys.stderr)
        return 64

    pending = to_write(plans)
    if not pending:
        return 0
    print("")
    print("Check only. A GeoJSON file is never written. To resync %d row(s), "
          "run --layer on the layer itself with --apply under ArcGIS Pro."
          % len(pending))
    return 1


# ------------------------------------------------------------------ geodatabase

def _import_arcpy():
    """Import arcpy only when a real feature class is about to be read."""
    try:
        import arcpy
    except RuntimeError as exc:
        # arcpy raises this on import when Pro cannot check out a licence.
        print("error: arcpy is installed but could not start: %s. Sign in "
              "to ArcGIS Pro or check its licence, then run again."
              % printable(str(exc)), file=sys.stderr)
        return None
    except ImportError:
        # Exit 3, not the 1 that sys.exit(message) gives. A scheduled check
        # run under the wrong Python must not report drift found every night.
        print("error: arcpy was not found. Run this with the Python that "
              "ships with ArcGIS Pro:\n"
              '  "%s" xydrift.py\n'
              "or the propy.bat in ...\\Pro\\bin\\Python\\Scripts\\.\n"
              "Only --self-test and --from-geojson run without arcpy."
              % PRO_PYTHON, file=sys.stderr)
        return None
    return arcpy


def layer_magnitude(arcpy, layer, sr, geographic):
    """Largest coordinate this layer holds in sr, for the Single-precision check.

    sr is --wkid, the system the stored columns are compared in. A geographic
    one is bounded by the globe, so 180 is the worst case and no read is
    needed. A projected one has no such bound, and a Single column's step
    grows with the coordinate: near 600000 ft it is 0.0625 ft, coarser than
    any tolerance worth using. The extent is stored metadata, so asking for it
    costs no second pass over the rows.

    The extent is projected into sr first. Describe reports it in the layer's
    own system, and a lon/lat layer with Single columns in state plane feet
    read as magnitude 82: the check passed, and every row drifted for ever.
    """
    if geographic:
        return 180.0
    extent = arcpy.Describe(layer).extent.projectAs(sr)
    corners = [abs(v) for v in
               (extent.XMin, extent.XMax, extent.YMin, extent.YMax)
               if is_number(v)]
    # An empty layer reports its corners as NaN. It also has no row that can
    # be wrong, so a magnitude of zero refuses nothing.
    return max(corners) if corners else 0.0


def layer_profile(arcpy, layer, wkid):
    """The spatial reference to compare in, and the layer's column types."""
    sr = arcpy.SpatialReference(wkid)
    geographic = sr.type == "Geographic"
    meters_per_unit = None if geographic else getattr(sr, "metersPerUnit", None)
    types = dict((f.name, f.type) for f in arcpy.ListFields(layer))
    return sr, geographic, meters_per_unit, types


def datum_transformation(arcpy, layer, sr):
    """The transformation Pro's own tools pick into sr, or None if none applies.

    A NAD83 state plane layer compared in WGS84 lon/lat crosses a datum.
    Calculate Geometry Attributes applies the first listed transformation
    when it fills the columns, but a SearchCursor applies none unless told.
    Without it every unmoved row of such a layer read as DRIFT by about
    0.5 m, and --apply would rewrite the whole layer.
    """
    desc = arcpy.Describe(layer)
    source = desc.spatialReference
    if source.GCS.name == sr.GCS.name:
        return None
    names = arcpy.ListTransformations(source, sr, desc.extent)
    return names[0] if names else None


def scan(arcpy, layer, x_field, y_field, sr, tolerance, wrap,
         transformation=None):
    """Read every row once and plan it. Opens no update cursor.

    ponytail: every plan is held in memory and there is no where clause, so a
    few hundred thousand rows are fine and tens of millions are not. Chunk by
    OID range if a layer outgrows it.
    """
    fields = ["OID@", x_field, y_field, "SHAPE@X", "SHAPE@Y"]
    with arcpy.da.SearchCursor(layer, fields, spatial_reference=sr,
                               datum_transformation=transformation) as cursor:
        # The geometry token returns the full float64 projection of the
        # point. plan_rows rounds it to the grid before any comparison.
        return plan_rows(cursor, tolerance, wrap)


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
    try:
        code, plans = check_layer(args, arcpy)
    except Exception as exc:
        # A bad --wkid, a dropped connection or a schema lock raises from
        # arcpy. Uncaught, that traceback exits 1, which reads as drift found.
        print("error: reading %s failed: %s. Nothing was written."
              % (args.layer, exc), file=sys.stderr)
        return 3
    if code is not None:
        return code

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


def column_refusal(arcpy, args, types, sr, geographic, tolerance):
    """Reason the two columns cannot be compared in sr, or None.

    A column that is not in the layer, that is not a number, or that is a
    Single too coarse for the tolerance at the layer's magnitude in sr.
    """
    for field in (args.x_field, args.y_field):
        if field not in types:
            return "column %s is not in %s" % (field, args.layer)
    magnitude = layer_magnitude(arcpy, args.layer, sr, geographic)
    for field in (args.x_field, args.y_field):
        reason = refuse_coord_field(field, types[field], tolerance, magnitude)
        if reason is not None:
            return reason
    return None


def check_layer(args, arcpy):
    """The read pass: every refusal, the scan and the report. Writes nothing.

    Returns (exit code, None) for a refusal, or (None, plans).
    """
    reason = same_field_refusal(args)
    if reason is not None:
        print("error: %s" % reason, file=sys.stderr)
        return 64, None
    if not arcpy.Exists(args.layer):
        print("error: layer does not exist: %s" % args.layer, file=sys.stderr)
        return 64, None

    sr, geographic, meters_per_unit, types = layer_profile(
        arcpy, args.layer, args.wkid)

    tolerance = args.tolerance
    units = args.tolerance_units
    if tolerance is None:
        if not geographic:
            print("error: a projected layer has no sane default tolerance. "
                  "Pass --tolerance in the layer's own linear units.",
                  file=sys.stderr)
            return 64, None
        tolerance = DEFAULT_TOLERANCE_DEGREES
        units = "degrees"
    try:
        tolerance = resolve_tolerance(tolerance, units, geographic,
                                      meters_per_unit)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64, None

    reason = column_refusal(arcpy, args, types, sr, geographic, tolerance)
    if reason is not None:
        print("error: %s" % reason, file=sys.stderr)
        return 64, None

    units_label = "degrees" if geographic else "linear units"
    transformation = datum_transformation(arcpy, args.layer, sr)
    if transformation is not None:
        print("datum transformation: %s (the default Pro's tools pick)"
              % transformation)
    plans = scan(arcpy, args.layer, args.x_field, args.y_field, sr,
                 tolerance, geographic, transformation)
    for line in describe(plans, tolerance, units_label, args.limit):
        print(line)
    if nothing_compared(plans):
        print("error: no row has a geometry, so nothing was checked.",
              file=sys.stderr)
        return 64, None

    return None, plans


# ------------------------------------------------------------- --infer-crs

def wkid_list(text):
    """The --infer-crs value: EPSG codes split by commas, or [] for the defaults.

    A code given twice is tested once, in the place it was first given.
    """
    if not text.strip():
        return []
    try:
        codes = [int(part) for part in text.split(",")]
    except ValueError:
        codes = [0]
    if any(code <= 0 for code in codes):
        raise argparse.ArgumentTypeError(
            "%r is not a list of EPSG codes split by commas, such as "
            "4326,2237" % text)
    return list(dict.fromkeys(codes))


def infer_refusal(args):
    """Reason the flags cannot go with --infer-crs, or None."""
    for given, why in (
            (args.apply,
             "--apply does not apply to --infer-crs, which only reads. Name "
             "the system first, then run the check with --wkid."),
            (args.wkid != DEFAULT_WKID,
             "--wkid does not apply to --infer-crs. It states the system of "
             "the columns, and --infer-crs finds it."),
            (args.xy_crs is not None,
             "--xy-crs does not apply to --infer-crs. It states the system of "
             "the columns, and --infer-crs finds it."),
            (args.tolerance_units == "degrees",
             "--tolerance-units degrees does not apply to --infer-crs. A "
             "degree is a different distance in every candidate, so give the "
             "tolerance in meters or feet."),
            (len(args.infer_crs) == 1,
             "--infer-crs ranks two or more systems, and was given only "
             "EPSG %s." % ",".join("%d" % c for c in args.infer_crs))):
        if given:
            return why
    return same_field_refusal(args)


def finish_inference(found, lines, next_step):
    """Print the report and the verdict. 0 names a system, 64 refuses."""
    for line in lines:
        print(line)
    print("")
    winner = found["winner"]
    if winner is None:
        # The table first, then the reason, even when both go to one pipe.
        sys.stdout.flush()
        print("error: no single system can be named: %s" % found["reason"],
              file=sys.stderr)
        return 64
    print("result: the stored columns are in EPSG %d." % winner)
    others = len(found["compared"]) - len(found["agree"][winner])
    if others:
        print("%d compared row(s) do not agree with EPSG %d. They have "
              "drifted, or hold another system." % (others, winner))
    print(next_step)
    return 0


def infer_layer(args, arcpy, value, units):
    """--infer-crs on a layer: one read per candidate, never a write."""
    if not arcpy.Exists(args.layer):
        print("error: layer does not exist: %s" % args.layer, file=sys.stderr)
        return 64
    own = arcpy.Describe(args.layer).spatialReference.factoryCode
    # A layer in an unknown or custom system reports 0, which is no candidate.
    candidates = args.infer_crs or list(dict.fromkeys(
        code for code in (own, GEOJSON_WKID, WEB_MERCATOR_WKID) if code))
    fields = ["OID@", args.x_field, args.y_field, "SHAPE@X", "SHAPE@Y"]
    results = []
    notes = {}
    read = 0
    for wkid in candidates:
        sr, geographic, meters_per_unit, types = layer_profile(
            arcpy, args.layer, wkid)
        try:
            tolerance = resolve_tolerance(value, units, geographic,
                                          meters_per_unit)
        except ValueError as exc:
            reason = "%s" % exc
        else:
            reason = column_refusal(arcpy, args, types, sr, geographic,
                                    tolerance)
        if reason is not None:
            print("error: EPSG %d: %s" % (wkid, reason), file=sys.stderr)
            return 64
        # The same transformation the check itself would read through.
        transformation = datum_transformation(arcpy, args.layer, sr)
        if transformation is not None:
            notes[wkid] = "datum transformation %s" % transformation
        with arcpy.da.SearchCursor(args.layer, fields, spatial_reference=sr,
                                   datum_transformation=transformation) as cur:
            read, compared, agree = candidate_agreement(cur, tolerance,
                                                        geographic)
        results.append((wkid, compared, agree))
    found = infer(results)
    lines = describe_inference(found, read, "%g %s" % (value, units), notes,
                               None, args.limit)
    return finish_inference(found, lines, "Next: check the layer for drift "
                            "with --wkid %s." % found["winner"])


def infer_geojson(args, value, units):
    """--infer-crs on a GeoJSON file, in 4326 and 3857 only. Never writes."""
    candidates = args.infer_crs or list(FILE_CANDIDATES)
    foreign = [code for code in candidates if code not in FILE_CANDIDATES]
    if foreign:
        print("error: --infer-crs on a file can test only EPSG 4326 and 3857, "
              "because this mode cannot reproject. Refused: %s. Run --layer "
              "with --infer-crs under ArcGIS Pro for any other system."
              % _epsg(foreign), file=sys.stderr)
        return 64
    try:
        tolerances = dict((code, resolve_tolerance(
            value, units, code == GEOJSON_WKID, 1.0)) for code in candidates)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    try:
        doc, stamp = load_geojson(args.from_geojson)
        reason = geojson_crs_refusal(doc)
        if reason is not None:
            raise ValueError(reason)
        # Not geojson_rows: its check that the columns are degrees is the
        # very question being asked here.
        rows = feature_rows(doc, args.x_field, args.y_field)
    except (OSError, ValueError, RecursionError, MemoryError) as exc:
        print("error: %s: %s" % (printable(args.from_geojson), exc),
              file=sys.stderr)
        return 64

    # Keyed by position, because a file's ids need not be unique or hashable.
    labels = dict((index, row[0]) for index, row in enumerate(rows, 1))
    results = []
    for code in candidates:
        keyed = []
        for index, (_, sx, sy, gx, gy) in enumerate(rows, 1):
            if code == WEB_MERCATOR_WKID:
                gx, gy = web_mercator(gx, gy)
            keyed.append((index, sx, sy, gx, gy))
        _, compared, agree = candidate_agreement(keyed, tolerances[code],
                                                 code == GEOJSON_WKID)
        results.append((code, compared, agree))
    found = infer(results)
    print("source: %s, file modified %s. A file is a snapshot, not the live "
          "layer." % (printable(args.from_geojson), stamp))
    lines = describe_inference(found, len(rows), "%g %s" % (value, units),
                               None, labels, args.limit)
    if found["winner"] == GEOJSON_WKID:
        step = ("Next: check the file for drift with --xy-crs 4326.")
    else:
        step = ("This mode compares only in EPSG 4326. Check the layer itself "
                "under ArcGIS Pro with --layer and --wkid %s."
                % found["winner"])
    return finish_inference(found, lines, step)


def run_infer(args):
    """The whole --infer-crs run. Read-only in both modes."""
    reason = infer_refusal(args)
    if reason is not None:
        print("error: %s" % reason, file=sys.stderr)
        return 64
    value = args.tolerance
    if value is None:
        value = DEFAULT_INFER_TOLERANCE_METERS
    units = args.tolerance_units or "meters"
    if args.from_geojson:
        return infer_geojson(args, value, units)
    arcpy = _import_arcpy()
    if arcpy is None:
        return 3
    try:
        return infer_layer(args, arcpy, value, units)
    except Exception as exc:
        # A candidate arcpy cannot build or project into raises here.
        # Uncaught, the traceback exits 1.
        print("error: reading %s failed: %s. Nothing was written."
              % (args.layer, exc), file=sys.stderr)
        return 3


# ------------------------------------------------------------------ self-test

class _StubField(object):
    """The two attributes of an arcpy field object that this tool reads."""

    def __init__(self, name, kind):
        self.name = name
        self.type = kind


class _StubExtent(object):
    """arcpy's extent object in the four corners and the one method used here.

    The corners are in the layer's own system, 4326. projectAs knows only the
    systems a test names, so code that reads the corners without projecting
    them gets the lon/lat numbers, as it would from real arcpy.
    """

    def __init__(self, xmin, ymin, xmax, ymax, projections=None):
        self.XMin = xmin
        self.YMin = ymin
        self.XMax = xmax
        self.YMax = ymax
        self._projections = dict(projections or {})

    def projectAs(self, sr):
        return self._projections[sr.factoryCode]


class _StubDescribe(object):
    """arcpy.Describe's result in the two attributes this tool reads."""

    def __init__(self, extent, wkid):
        self.extent = extent
        self.spatialReference = _StubSpatialReference(wkid)


class _StubSpatialReference(object):
    """arcpy.SpatialReference in the three attributes used here."""

    def __init__(self, wkid):
        self.factoryCode = wkid
        geographic = wkid in (4326, 4269)
        self.type = "Geographic" if geographic else "Projected"
        self.metersPerUnit = (None if geographic else
                              1.0 if wkid == 3857 else 0.3048006096012192)
        self.GCS = _StubField("GCS_North_American_1983" if wkid in (2237, 4269)
                              else "GCS_WGS_1984", "GCS")


class _StubCursor(object):
    """arcpy.da.SearchCursor and UpdateCursor, in the surface used here."""

    def __init__(self, table, fields, sr, writable, transformation=None):
        self._table = table
        self._fields = list(fields)
        self._sr = sr
        # A read across a datum with no transformation lands about 0.5 m off,
        # as the real cursor does for NAD83 geometry read in WGS84.
        self._shift = 0.0
        if sr is not None and transformation is None and (
                _StubSpatialReference(table.layer_wkid).GCS.name
                != sr.GCS.name):
            self._shift = 5e-6
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
            return geom[code][0 if field == "SHAPE@X" else 1] + self._shift
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

    def SearchCursor(self, layer, fields, spatial_reference=None,
                     datum_transformation=None):
        return _StubCursor(self._table, fields, spatial_reference, False,
                           datum_transformation)

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
        # The rows hold 2237 geometry at 1000 times the lon/lat.
        self.extent = _StubExtent(-82.7, 29.2, -82.1, 29.8, {
            2237: _StubExtent(-82700.0, 29200.0, -82100.0, 29800.0)})
        # The layer's own system. The rows' 4326 geometry is what a cursor
        # returns after any datum transformation.
        self.layer_wkid = 4326
        # What ListTransformations offers between two datums, first as default.
        self.transformations = ["WGS_1984_(ITRF00)_To_NAD_1983",
                                "NAD_1983_To_WGS_1984_5"]
        self.da = _StubDa(self)

    def Exists(self, path):
        return path == self.FC

    def Describe(self, layer):
        return _StubDescribe(self.extent, self.layer_wkid)

    def ListFields(self, layer):
        return [_StubField(n, t) for n, t in sorted(self.types.items())]

    def SpatialReference(self, wkid):
        return _StubSpatialReference(wkid)

    def ListTransformations(self, source, target, extent=None):
        return list(self.transformations)


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


def _infer_stub(systems):
    """A stub layer in 4326 whose rows store X and Y in the systems named.

    One row per entry. None leaves both columns empty. The 2237 and 3857
    geometry is what a cursor returns in that system: 2237 is the stub's
    usual 1000 times the lon/lat, and 3857 is the closed form.
    """
    rows = []
    for oid, wkid in enumerate(systems, 1):
        lon, lat = -82.0 - oid / 100.0, 29.0 + oid / 100.0
        geom = {4326: (lon, lat), 4269: (lon, lat),
                2237: (lon * 1000.0, lat * 1000.0),
                3857: web_mercator(lon, lat)}
        sx, sy = geom[wkid] if wkid else (None, None)
        rows.append({"OBJECTID": oid, "X": sx, "Y": sy, "LABEL": "i%d" % oid,
                     "geom": geom})
    stub = _StubArcpy(rows)
    stub.extent = _StubExtent(-82.7, 29.0, -82.0, 29.7, {
        2237: _StubExtent(-82700.0, 29000.0, -82000.0, 29700.0),
        3857: _StubExtent(*(web_mercator(-82.7, 29.0)
                            + web_mercator(-82.0, 29.7)))})
    return stub


def _stub_geojson(rows=None):
    """The same seven rows as a GeoJSON FeatureCollection, as a file holds them."""
    features = []
    for row in _stub_rows() if rows is None else rows:
        geom = row["geom"]
        features.append({
            "type": "Feature", "id": row["OBJECTID"],
            "geometry": None if geom is None else {
                "type": "Point", "coordinates": list(geom[4326])},
            "properties": {"X": row["X"], "Y": row["Y"],
                           "LABEL": row["LABEL"]}})
    return {"type": "FeatureCollection", "features": features}


def _args(**kwargs):
    """Parsed arguments for a stub run, with the defaults filled in."""
    argv = ["--layer", _StubArcpy.FC]
    for key, value in sorted(kwargs.items()):
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        else:
            argv.extend([flag, str(value)])
    return _parse(argv)


def _harness():
    """check() and raises(), and the pass count and failure list they share."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, kind=ValueError):
        try:
            fn()
        except kind:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    return check, raises, passed, failed


def _footer(passed, failed):
    """The self-test's closing lines and its exit code."""
    total = passed + len(failed)
    if not failed:
        return ["%d assertions, 0 failed" % total], 0
    return (["%d assertions, %d failed" % (total, len(failed))]
            + ["  FAILED: %s" % f for f in failed]), 1


def self_test():
    """Assertions over the decision core. No arcpy, no database, no network."""
    import contextlib
    import io
    import shutil
    import tempfile
    from types import ModuleType
    from unittest import mock

    check, raises, passed, failed = _harness()

    def run_stub(stub, **kwargs):
        """run() against the stub, returning (exit code, printed text)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with contextlib.redirect_stderr(out):
                code = run(_args(**kwargs), stub)
        return code, out.getvalue()

    def is_ascii(text):
        return all(ord(c) < 128 for c in text)

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
    check(wrapped_delta(-95.0, 90.0) == 175.0,
          "a 185 degree gap measures 175, so the fold is at exactly 180")
    check(wrapped_delta(-180.0, 0.5) == 179.5,
          "a 180.5 degree gap measures 179.5, so the fold is at 180 and not "
          "past it")
    check(wrapped_delta(82.2, -277.8) == 360.0,
          "a geometry X past -180 is measured plainly too, not wrapped")
    check(plan_row(1, 180.000009, 10.0, -179.999992, 10.0, tol, True).dx
          == 360.000001,
          "a stored X 9e-6 past 180 is outside the tolerance of the meridian, "
          "so it is measured plainly and not wrapped")
    check(wrapped_delta(-100.0, 100.0) == 160.0,
          "a 200 degree gap measures 160 the short way")
    check(wrapped_delta(0.0, 400.0) == 400.0,
          "a value past 180 is not wrapped, so 400 against 0 measures 400")
    check(wrapped_delta(-7282.2, -82.2) > 7000.0,
          "a stored X twenty whole turns off is not the same meridian  <-- pinned defect")
    check(wrapped_delta(277.8, -82.2) == 360.0,
          "277.8 in the 0-360 convention is 360 from -82.2, not a match  <-- pinned defect")
    check(wrapped_delta(180.000000001, -180.0) < 1e-8,
          "a writer's noise past 180 is still wrapped, because the seam is "
          "tested on the rounding grid")
    check(delta(0.0, 400.0) == 400.0, "without wrapping the same pair measures 400")
    check(wrapped_delta(180.0000001, -180.0, tol) < tol,
          "a stored X 1e-7 past 180 is inside the tolerance of the meridian, "
          "so it still wraps  <-- pinned defect")
    seam_plans = [plan_row(1, 180.0000001, 10.0, gx, 10.0, tol, True)
                  for gx in (-180.0, 180.0)]
    check(all(p.verdict == OK and p.dx < tol for p in seam_plans),
          "the same stored X past the seam is OK against -180 and +180 "
          "alike, and measures 1e-7 on both  <-- pinned defect")
    seam = _StubArcpy([{"OBJECTID": 1, "X": 180.0000001, "Y": 10.0,
                        "LABEL": "a", "geom": {4326: (-180.0, 10.0)}}])
    code, _ = run_stub(seam, apply=True)
    check(code == 0 and seam.updates == 0
          and seam.rows[1]["X"] == 180.0000001,
          "--apply leaves that row alone, so it stamps no editor tracking  "
          "<-- pinned defect")

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
    check(plan_rows([(1, -82.1, 10.0, -82.10004, 10.0)], tol, True)[0].verdict
          == DRIFT,
          "a 4 m move survives the rounding, so the grid is 8 decimals and "
          "not fewer")
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
    check(worst(FILL, DRIFT) == DRIFT and worst(DRIFT, FILL) == DRIFT,
          "a drifted column outranks one to fill, in either order")
    check(plan_row(1, -82.4, None, -82.3999, 29.5, tol, True).verdict == DRIFT,
          "a row with a drifted X and an empty Y is counted as DRIFT")
    upside = plan_row(1, 0.0, 180.0, 0.0, -180.0, tol, True)
    check(upside.verdict == DRIFT and upside.dy == 360.0,
          "latitude is never wrapped, even with wrapping on, so a Y of 180 "
          "against -180 is 360 apart")
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
    raises(lambda: resolve_tolerance(float("nan"), "degrees", True),
           "a NaN tolerance is refused, because no drift is ever greater "
           "than it  <-- pinned defect")
    raises(lambda: resolve_tolerance(float("inf"), None, True),
           "an infinite tolerance is refused  <-- pinned defect")
    raises(lambda: resolve_tolerance(float("1e400"), None, False, 1.0),
           "1e400, which reads as infinity, is refused on a projected layer "
           "too")
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
    check("... and" not in "\n".join(describe(many[:3], tol, "degrees", 3)),
          "exactly as many rows as the limit need no truncation line, not "
          "an '... and 0 more'")
    listed = [l for l in describe(many, tol, "degrees", 3) if l.startswith("  OID")]
    check(len(listed) == 3,
          "the limit is how many rows are listed, not only what the tail line "
          "claims  <-- pinned defect")
    clean_text = "\n".join(describe(plans[:3], tol, "degrees"))
    check("Every stored coordinate agrees" in clean_text,
          "a clean layer gets a sentence, not an empty list")
    mixed_text = "\n".join(describe([plans[0], plans[5]], tol, "degrees"))
    check("1 row(s) have no geometry" in mixed_text,
          "a clean report still counts the rows it could not compare  <-- pinned defect")
    check("Every stored coordinate that has a geometry agrees" in mixed_text
          and "agrees with its geometry" not in mixed_text,
          "and does not claim that every coordinate agrees")
    shapeless_text = "\n".join(describe([plans[5], plans[5]], tol, "degrees"))
    check("no stored coordinate was compared" in shapeless_text
          and "agrees" not in shapeless_text
          and "2 row(s) have no geometry" in shapeless_text,
          "rows that all lack a geometry are reported as not compared, not as "
          "clean  <-- pinned defect")
    check(nothing_compared([plans[5]]) and not nothing_compared(plans)
          and not nothing_compared([]),
          "nothing was compared only when there were rows and none had a "
          "geometry")
    check(_fmt(None) == "none" and _fmt(0.0001) == "0.0001",
          "a measured drift prints as a number and an absent one as none")
    check(printable(4) == "4" and printable("a-1") == "a-1",
          "an ordinary id prints as itself")
    check(printable(u"\u6771\ud800") == "\\u6771\\ud800",
          "a CJK id and a lone surrogate print escaped in pure ASCII")
    check(printable("4\n\r\t\x1b[8m\x7f~") == "4\\x0a\\x0d\\x09\\x1b[8m\\x7f~",
          "newline, CR, tab, ESC and DEL in an id print escaped, so an id "
          "cannot forge or hide a report line  <-- pinned defect")
    odd = [plan_row(u"\u6771\u4eac", -82.4, 29.5, -82.3999, 29.5, tol),
           plan_row(u"\ud800", -82.4, 29.5, -82.3999, 29.5, tol)]
    odd_text = "\n".join(describe(odd, tol, "degrees"))
    check("OID \\u6771\\u4eac DRIFT" in odd_text
          and "OID \\ud800 DRIFT" in odd_text and is_ascii(odd_text),
          "the report prints ids no console can encode as escapes, not as a "
          "traceback  <-- pinned defect")

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
    sr2237 = stub.SpatialReference(2237)

    def state_plane_extent():
        """A lon/lat layer whose extent in 2237 is state plane feet."""
        return _StubExtent(-82.7, 29.2, -82.1, 29.8, {2237: _StubExtent(
            560000.0, 1740000.0, 640000.0, 1890000.0)})

    check(layer_magnitude(stub, _StubArcpy.FC, sr, True) == 180.0,
          "a geographic layer is bounded by the globe, so nothing is read")
    check(layer_magnitude(stub, _StubArcpy.FC, sr2237, False) == 82700.0,
          "a projected --wkid reports the largest corner of the extent in "
          "that system, not the layer's own lon/lat corners  <-- pinned defect")
    wide = _StubArcpy(_stub_rows())
    wide.extent = state_plane_extent()
    check(layer_magnitude(wide, _StubArcpy.FC, sr2237, False) == 1890000.0,
          "a state plane extent reports its largest coordinate, not its "
          "easting  <-- pinned defect")
    nan = float("nan")
    empty_extent = _StubArcpy(_stub_rows())
    empty_extent.extent = _StubExtent(nan, nan, nan, nan, {
        2237: _StubExtent(nan, nan, nan, nan)})
    check(layer_magnitude(empty_extent, _StubArcpy.FC, sr2237, False) == 0.0,
          "an empty layer reports NaN corners, as real arcpy does, and "
          "refuses no column for them  <-- pinned defect")

    scanned = scan(stub, _StubArcpy.FC, "X", "Y", sr, tol, True)
    check(len(scanned) == 7, "the scan reads every row once")
    check(stub.opened[-1][1] == 4326,
          "the scan passes the requested spatial reference to the cursor")
    check(stub.opened[-1][2] is False, "the scan opens no writing cursor")

    # ---- a NAD83 layer compared in WGS84 lon/lat
    check(datum_transformation(stub, _StubArcpy.FC, sr) is None,
          "a layer already on the comparison datum needs no transformation")
    nad83 = _StubArcpy([r for r in _stub_rows() if r["OBJECTID"] in (1, 2, 3)])
    nad83.layer_wkid = 2237
    check(datum_transformation(nad83, _StubArcpy.FC, sr)
          == "WGS_1984_(ITRF00)_To_NAD_1983",
          "a NAD83 layer read in WGS84 takes the first listed transformation, "
          "the one Calculate Geometry Attributes uses")
    code, text = run_stub(nad83)
    check(code == 0 and "DRIFT             0" in text
          and "WGS_1984_(ITRF00)_To_NAD_1983" in text,
          "unmoved rows of a NAD83 layer read in WGS84 are OK, not a 0.5 m "
          "DRIFT on every row, and the report names the transformation  "
          "<-- pinned defect")
    nad83.transformations = []
    check(datum_transformation(nad83, _StubArcpy.FC, sr) is None,
          "a datum pair with no listed transformation is read untransformed, "
          "as Pro's own tools read it")
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
    raises(lambda: resync(stub, _StubArcpy.FC, plans, "X", "Y",
                          workspace="stub.gdb"),
           "a refused write is raised, not swallowed", RuntimeError)
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
    raises(lambda: resync(stub, _StubArcpy.FC, plans, "X", "Y"),
           "a refused write without an edit session is raised too",
           RuntimeError)

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
    code, out = run_stub(stub, tolerance="nan", apply=True)
    check(code == 64 and "not a distance" in out and stub.updates == 0,
          "--tolerance nan with --apply is refused before anything is "
          "written, instead of reporting a clean layer  <-- pinned defect")
    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, wkid=3857, tolerance=1.0, apply=True)
    check(code == 3 and "reading stub.gdb/Points failed" in out
          and "Nothing was written" in out and stub.updates == 0,
          "an arcpy error while the layer is read exits 3, not the 1 that "
          "means drift found  <-- pinned defect")

    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, x_field="LON")
    check(code == 64 and "is not in" in out, "a column that is not there is refused")
    # A Single easting column in state plane feet, checked to a hundredth of a
    # foot. The column's own step is 0.0625 ft, so every row would read as
    # drifted for ever, and no tolerance the caller can pick changes that.
    # The layer itself is lon/lat, and the columns hold state plane feet.
    single = _StubArcpy(_stub_rows(),
                        types={"OBJECTID": "OID", "X": "Single",
                               "Y": "Single", "LABEL": "String"})
    single.extent = state_plane_extent()
    code, out = run_stub(single, wkid=2237, tolerance=0.01)
    check(code == 64 and "coarser than the tolerance" in out,
          "a Single column is refused from the extent in --wkid, on a lon/lat "
          "layer whose columns hold state plane feet  <-- pinned defect")
    check(single.opened == [],
          "and that refusal also happens before any cursor opens")
    single = _StubArcpy(_stub_rows(),
                        types={"OBJECTID": "OID", "X": "Single",
                               "Y": "Single", "LABEL": "String"})
    single.extent = state_plane_extent()
    code, out = run_stub(single, wkid=2237, tolerance=1.0)
    check(code == 1, "the same layer at a one foot tolerance runs")

    stub = _StubArcpy(_stub_rows())
    code, out = run_stub(stub, x_field="LABEL")
    check(code == 64 and "compared as a number" in out,
          "a String column is refused before any cursor opens")
    check(stub.opened == [],
          "and the refusal happens before the layer is read  <-- pinned defect")

    nan_rows = _stub_rows()[:3]
    nan_rows[1]["X"] = float("nan")
    code, out = run_stub(_StubArcpy(nan_rows))
    check(code == 1 and "OID 2 DRIFT: dx=nan" in out
          and "Every stored" not in out,
          "a NaN column read from a layer is drift and exits 1, not a clean "
          "layer  <-- pinned defect")
    nan_stub = _StubArcpy(nan_rows)
    code, out = run_stub(nan_stub, apply=True)
    check(code == 0 and nan_stub.rows[2]["X"] == -82.2
          and nan_stub.rows[2]["Y"] == 29.3 and nan_stub.updates == 1,
          "and --apply writes the geometry's X over the NaN and nothing else")
    nan_geom = _stub_rows()[:1]
    nan_geom[0]["X"] = -99.0
    nan_geom[0]["geom"] = {4326: (float("nan"), float("nan"))}
    code, out = run_stub(_StubArcpy(nan_geom))
    check(code == 64 and "NO_GEOMETRY       1" in out
          and "nothing was checked" in out and "Every stored" not in out,
          "a layer whose only point is NaN compared nothing, and exits 64, "
          "not 0  <-- pinned defect")
    shapeless_stub = _StubArcpy(_stub_rows()[5:6])
    code, out = run_stub(shapeless_stub, apply=True)
    check(code == 64 and "no stored coordinate was compared" in out
          and shapeless_stub.updates == 0,
          "a layer with no geometry on any row exits 64 and writes nothing, "
          "even with --apply  <-- pinned defect")

    missing = _StubArcpy(_stub_rows())
    out = io.StringIO()
    with contextlib.redirect_stderr(out):
        code = run(_parse(["--layer", "nowhere.gdb/Points"]), missing)
    check(code == 64 and "does not exist" in out.getvalue(),
          "a layer that does not exist is refused")

    # ---- the GeoJSON reader, pure parts first
    check(rounded(None) is None, "absent geometry stays absent on the grid")
    check(rounded(-82.100000004) == -82.1,
          "a geometry coordinate is rounded to the 8 decimal grid")
    check(is_number(1.5) and is_number(-82), "a float and an integer are numbers")
    check(not is_number(True), "a JSON true is not a coordinate")
    check(not is_number("-82.1"), "a numeric string is not a number")
    check(not is_number(None), "a null is not a number")
    check(not is_number(float("nan")) and not is_number(float("inf")),
          "NaN and infinity are not finite numbers")
    check(not is_number(10 ** 400),
          "an integer too large for a float is refused, not overflowed")
    check(classify_pair(float("nan"), -82.1, tol) == DRIFT,
          "the core calls a NaN column drift, not clean, so no reader has "
          "to remember to refuse it  <-- pinned defect")
    nan_row = plan_row(1, float("nan"), 29.2, -82.1, 29.2, tol, True)
    check(nan_row.verdict == DRIFT and nan_row.new_x == -82.1
          and nan_row.new_y is None,
          "a NaN X with wrapping on is drift, and the resync would write the "
          "geometry's X over it")
    check(classify_pair(float("inf"), -82.1, tol, True) == DRIFT,
          "an infinite column is drift as well")
    check(geometry_xy(float("nan"), float("nan")) == (None, None),
          "a NaN geometry is no geometry, not a clean row  <-- pinned defect")
    check(geometry_xy(float("nan"), 29.2) == (None, None)
          and geometry_xy(-82.1, float("inf")) == (None, None)
          and geometry_xy(None, 29.2) == (None, None)
          and geometry_xy(-82.1, None) == (None, None),
          "a geometry with one axis missing or not finite is no geometry "
          "either, so no half of it is written")
    check(geometry_xy(-82.100000004, 29.2) == (-82.1, 29.2),
          "a finite geometry is rounded to the grid")

    check(unique_names([("X", 1.0), ("Y", 2.0)]) == {"X": 1.0, "Y": 2.0},
          "an object with distinct names reads as a dict")
    raises(lambda: unique_names([("X", -82.4), ("Y", 29.5), ("X", -82.3999)]),
           "a name that appears twice in one object is refused  <-- pinned defect")

    check(xy_crs_refusal(4326) is None, "--xy-crs 4326 is accepted")
    no_crs = xy_crs_refusal(None)
    check(no_crs is not None and "--xy-crs" in no_crs,
          "a file run with no stated column system is refused and says which "
          "flag to add")
    state_plane = xy_crs_refusal(2237)
    check(state_plane is not None and "cannot reproject" in state_plane,
          "stored columns in state plane are refused, not compared with "
          "lon/lat geometry  <-- pinned defect")
    check("2237" in state_plane, "the refusal names the code it was given")
    check(xy_crs_refusal(3857) is not None,
          "web mercator metres are refused as well")
    check(xy_crs_refusal(4269) is not None and xy_crs_refusal(4267) is not None,
          "NAD83 and NAD27 lon/lat are refused too, because they are not the "
          "WGS84 the geometry is in and nothing here shifts a datum  <-- pinned defect")

    check(geojson_crs_refusal({"type": "FeatureCollection"}) is None,
          "a file with no crs member is RFC 7946 lon/lat")
    check(geojson_crs_refusal({"crs": {"type": "name", "properties": {
        "name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}}) is None,
          "the CRS84 name GDAL writes is accepted")
    check(geojson_crs_refusal({"crs": {"type": "name", "properties": {
        "name": " epsg:4326 "}}}) is None,
          "the name is compared without regard to case or spaces")
    projected_crs = geojson_crs_refusal({"crs": {"type": "name", "properties": {
        "name": "urn:ogc:def:crs:EPSG::2237"}}})
    check(projected_crs is not None and "2237" in projected_crs,
          "a file that declares projected geometry is refused  <-- pinned defect")
    check(geojson_crs_refusal({"crs": {"type": "link", "properties": {
        "href": "http://example.invalid/crs"}}}) is not None,
          "a linked crs that names nothing is refused")
    check(geojson_crs_refusal({"crs": "EPSG:2237"}) is not None,
          "a crs member that is only a string is refused")
    check(geojson_crs_refusal({"crs": {"type": "name", "properties": {
        "name": "EPSG:43260"}}}) is not None,
          "a name that only starts with a lon/lat name is refused, so the "
          "match is whole")
    check(geojson_crs_refusal([]) is None,
          "a document that is not an object has no crs to refuse here")

    check(point_xy(None, 1) == (None, None), "a null geometry has no X or Y")
    check(point_xy({"type": "Point", "coordinates": []}, 1) == (None, None),
          "an empty point has no X or Y either")
    check(point_xy({"type": "Point", "coordinates": [-82.1, 29.2]}, 1)
          == (-82.1, 29.2), "a point gives its longitude and latitude")
    check(point_xy({"type": "Point", "coordinates": [-82.1, 29.2, 12.0]}, 1)
          == (-82.1, 29.2), "a third coordinate is height and is ignored")
    whole = point_xy({"type": "Point", "coordinates": [-82, 29]}, 1)
    check(whole == (-82.0, 29.0) and isinstance(whole[0], float),
          "integer coordinates are read as floats")
    check(point_xy({"type": "Point", "coordinates": [180.00000000001, 0]},
                   1)[0] == 180.00000000001,
          "a writer's noise past 180 is still on the meridian, not refused")
    check(point_xy({"type": "Point", "coordinates": [-180, -90]}, 1)
          == (-180.0, -90.0), "the corners of the globe are positions")
    raises(lambda: point_xy({"type": "LineString", "coordinates": []}, 3),
           "a line is refused, because only a point has one X and one Y")
    raises(lambda: point_xy("POINT (1 2)", 3),
           "a geometry that is not an object is refused")
    raises(lambda: point_xy({"type": "Point", "coordinates": [1]}, 3),
           "a point with one coordinate is refused")
    raises(lambda: point_xy({"type": "Point", "coordinates": "1 2"}, 3),
           "point coordinates that are not a list are refused")
    raises(lambda: point_xy({"type": "Point", "coordinates": None}, 3),
           "null point coordinates are refused, not read as an empty point")
    raises(lambda: point_xy({"type": "Point"}, 3),
           "a point with no coordinates member is refused as well")
    raises(lambda: point_xy({"type": "Point", "coordinates": ["-82.1", 29.2]},
                            3), "a text coordinate in the geometry is refused")
    raises(lambda: point_xy({"type": "Point",
                             "coordinates": [612345.25, 1740000.5]}, 3),
           "state plane feet in the geometry are refused, not read as "
           "degrees  <-- pinned defect")
    raises(lambda: point_xy({"type": "Point", "coordinates": [0.0, 90.5]}, 3),
           "a latitude past the pole is refused")
    raises(lambda: point_xy({"type": "Point", "coordinates": [180.5, 0.0]}, 3),
           "a longitude past 180, the 0-360 convention, is refused  "
           "<-- pinned defect")
    raises(lambda: point_xy({"type": "Point",
                             "coordinates": [float("nan"), float("nan")]}, 3),
           "a NaN geometry is refused, not planned as no geometry  "
           "<-- pinned defect")
    raises(lambda: point_xy({"type": "Point",
                             "coordinates": [-82.1, float("nan")]}, 3),
           "a NaN latitude alone is refused as well  <-- pinned defect")
    def refusal(fn):
        """The ValueError message fn raises, or "" when it raises none."""
        try:
            fn()
        except ValueError as exc:
            return str(exc)
        return ""
    check(refusal(lambda: None) == "",
          "a call that refuses nothing gives no reason, so a check on the "
          "reason cannot pass by accident")
    nad27 = {"type": "name", "properties": {
        "name": "urn:ogc:def:crs:EPSG::4267"}}
    crs84 = {"type": "name", "properties": {
        "name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}

    def nested_crs(where, crs):
        """One clean point with a crs member on the feature or the geometry."""
        doc = _stub_geojson(_stub_rows()[:1])
        target = doc["features"][0]
        if where == "geometry":
            target = target["geometry"]
        target["crs"] = crs
        return doc

    for where in ("feature", "geometry"):
        reason = refusal(lambda: feature_rows(nested_crs(where, nad27),
                                              "X", "Y"))
        check("4267" in reason
              and ("geometry of feature 1" in reason) == (where == "geometry"),
              "a NAD27 crs on the %s is refused, not compared with WGS84 "
              "columns as clean  <-- pinned defect" % where)
        check(len(feature_rows(nested_crs(where, crs84), "X", "Y")) == 1,
              "a CRS84 crs on the %s is lon/lat and is read" % where)
    refused = refusal(lambda: point_xy({"type": "MultiPoint",
                                        "coordinates": []}, 9))
    check("feature 9" in refused and "MultiPoint" in refused,
          "a refused geometry names the feature and its type")

    doc = _stub_geojson()
    rows = feature_rows(doc, "X", "Y")
    check(len(rows) == 7, "every feature becomes one row")
    check(rows[3] == (4, -82.4, 29.5, -82.3999, 29.5),
          "a row carries the id, the stored pair and the geometry pair")
    check(rows[5][3:] == (None, None),
          "a feature with null geometry reads as no geometry")
    single_feature = {"type": "Feature", "geometry": {
        "type": "Point", "coordinates": [1.0, 2.0]},
        "properties": {"X": 1.0, "Y": 2.0}}
    check(feature_rows(single_feature, "X", "Y") == [(1, 1.0, 2.0, 1.0, 2.0)],
          "a bare Feature is read as one row numbered 1")
    sparse = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": "a",
         "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
         "properties": {"X": 1.0, "Y": 2.0}},
        {"type": "Feature", "id": "b",
         "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
         "properties": {"Y": 2.0}},
        {"type": "Feature", "id": "c",
         "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
         "properties": None}]}
    sparse_rows = feature_rows(sparse, "X", "Y")
    check([r[0] for r in sparse_rows] == ["a", "b", "c"],
          "the OID of a file row is the feature's id member")
    idless = _stub_geojson()
    del idless["features"][2]["id"]
    check(feature_rows(idless, "X", "Y")[2][0] == 3,
          "a feature with no id takes its 1-based position")
    zero_id = _stub_geojson()
    zero_id["features"][2]["id"] = 0
    check(feature_rows(zero_id, "X", "Y")[2][0] == 0,
          "a feature whose id is 0 keeps it, not its position")
    check(sparse_rows[1][1] is None and sparse_rows[2][1:3] == (None, None),
          "an absent property and null properties read as empty columns")
    check([p.verdict for p in plan_rows(sparse_rows, tol, True)]
          == [OK, FILL, FILL],
          "and so they plan as FILL, the same as a null in a layer")
    empty = refusal(lambda: feature_rows(
        {"type": "FeatureCollection", "features": []}, "TYPO", "Y"))
    check("no features" in empty and "TYPO" in empty,
          "an empty collection is refused, because a misspelled column "
          "cannot be caught in it  <-- pinned defect")

    def mutated(field, value):
        bad = _stub_geojson()
        bad["features"][2]["properties"][field] = value
        return bad
    refused = refusal(lambda: feature_rows(mutated("X", "-82.3"), "X", "Y"))
    check("column X" in refused and "feature 3" in refused,
          "a coordinate stored as text is refused, not compared, and the "
          "refusal names the column and the feature  <-- pinned defect")
    raises(lambda: feature_rows(mutated("Y", True), "X", "Y"),
           "a coordinate stored as true is refused")
    raises(lambda: feature_rows(mutated("Y", float("inf")), "X", "Y"),
           "an infinite coordinate is refused")
    raises(lambda: feature_rows(doc, "LON", "Y"),
           "a column no feature carries is refused as a misspelling")
    raises(lambda: feature_rows({"type": "Point", "coordinates": [1, 2]},
                                "X", "Y"),
           "a bare geometry is not a feature collection")
    raises(lambda: feature_rows([], "X", "Y"),
           "a document that is a list is refused")
    raises(lambda: feature_rows({"type": "FeatureCollection"}, "X", "Y"),
           "a collection with no features list is refused")
    raises(lambda: feature_rows({"type": "FeatureCollection", "features": 5},
                                "X", "Y"),
           "a features member that is a number is refused, not a traceback "
           "that exits 1  <-- pinned defect")
    not_feature = refusal(lambda: feature_rows({
        "type": "FeatureCollection", "features": [{
            "type": "feature", "geometry": None,
            "properties": {"X": 1.0, "Y": 2.0}}]}, "X", "Y"))
    check("is not a Feature" in not_feature,
          "an item typed feature in lower case is refused as not a Feature, "
          "even when it carries both columns")
    raises(lambda: feature_rows({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": None, "properties": [1, 2]}]},
        "X", "Y"), "properties that are not an object are refused")
    not_object = refusal(lambda: feature_rows({
        "type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "a\nb", "geometry": None,
            "properties": []}]}, "X", "Y"))
    check("feature a\\x0ab has properties that are not an object" in not_object,
          "empty properties that are a list are refused, not read as an empty "
          "object, and the refusal escapes the id")

    check(lonlat_refusal(rows, "X", "Y") is None,
          "lon/lat columns pass the range check")
    feet = [(1, 612345.25, 1740000.5, -82.1, 29.2),
            (2, None, None, -82.2, 29.3)]
    feet_reason = lonlat_refusal(feet, "X", "Y")
    check(feet_reason is not None and "column X" in feet_reason,
          "columns in feet under --xy-crs 4326 are refused, because the "
          "caller's word was wrong  <-- pinned defect")
    check(lonlat_refusal([(1, -82.1, 1740000.5, -82.1, 29.2)], "X", "Y")
          is not None, "a Y column with no latitude in it is refused")
    check(lonlat_refusal([(1, -82.1, 120.0, -82.1, 29.2)], "X", "Y")
          is not None, "a Y column whose values are all past 90 is refused")
    check(lonlat_refusal([(1, 180.0, 90.0, 180.0, 0.0)], "X", "Y") is None,
          "exactly 180 and exactly 90 are still degrees")
    wild = [(1, -82.1, 29.2, -82.1, 29.2), (2, -8210.0, 29.3, -82.2, 29.3)]
    check(lonlat_refusal(wild, "X", "Y") is None,
          "one wild value among real longitudes is a drifted row, not a "
          "refusal")
    check(lonlat_refusal([(1, None, None, None, None)], "X", "Y") is None,
          "columns that are all empty have no system to disagree with")
    raises(lambda: geojson_rows({"type": "FeatureCollection", "crs": {
        "type": "name", "properties": {"name": "EPSG:2237"}},
        "features": []}, "X", "Y"),
        "the whole-file checks raise the crs refusal")
    raises(lambda: geojson_rows({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": None,
         "properties": {"X": 612345.25, "Y": 29.0}}]}, "X", "Y"),
        "and the range refusal")
    check(len(geojson_rows(doc, "X", "Y")) == 7,
          "a clean file passes all of them and gives its rows")

    # The claim the mode rests on: the same rows give the same plans whether
    # they come from the layer or from the file.
    stub = _StubArcpy(_stub_rows())
    from_layer = scan(stub, _StubArcpy.FC, "X", "Y",
                      stub.SpatialReference(4326), tol, True)
    from_file = plan_rows(rows, tol, True)
    same = [(a.oid, a.verdict, a.dx, a.dy, a.new_x, a.new_y)
            for a in from_layer] == [
        (b.oid, b.verdict, b.dx, b.dy, b.new_x, b.new_y) for b in from_file]
    check(same, "a file and a layer holding the same rows plan identically, "
          "down to the measured drift")
    noisy_file = feature_rows({"type": "Feature", "id": 1, "geometry": {
        "type": "Point", "coordinates": [-82.100000004, 29.200000004]},
        "properties": {"X": -82.1, "Y": 29.2}}, "X", "Y")
    check(plan_rows(noisy_file, 1e-9, True)[0].verdict == OK,
          "the file path rounds the geometry exactly as the scan does")

    # The antimeridian, read from a file.
    def one_point(stored_x, geom_x):
        return feature_rows({"type": "Feature", "id": 1, "geometry": {
            "type": "Point", "coordinates": [geom_x, 10.0]},
            "properties": {"X": stored_x, "Y": 10.0}}, "X", "Y")
    check(plan_rows(one_point(-180.0, 180.0), tol, True)[0].verdict == OK,
          "a stored -180 against a point at +180 is not drift in a file "
          "either  <-- pinned defect")
    check(plan_rows(one_point(179.9999999, -179.9999999), tol, True)[0]
          .verdict == OK, "two sides of the antimeridian 2e-7 apart are clean")
    across = plan_rows(one_point(179.99, -179.99), tol, True)[0]
    check(across.verdict == DRIFT and abs(across.dx - 0.02) < 1e-9,
          "a real move across the antimeridian measures 0.02 degrees, not "
          "359.98")

    # ---- the GeoJSON mode end to end, on real files. Everything below
    # writes into one temp directory and deletes it again.
    tmp = tempfile.mkdtemp(prefix="xydrift-selftest-")

    def tmpfile(name, content):
        path = os.path.join(tmp, name)
        if not isinstance(content, bytes):
            content = content.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def read_bytes(path):
        with open(path, "rb") as handle:
            return handle.read()

    def run_cli(argv):
        """main() with stdout and stderr captured together."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with contextlib.redirect_stderr(out):
                code = main(argv)
        return code, out.getvalue()

    def run_file(name, text, *extra):
        path = tmpfile(name, text)
        return run_cli(["--from-geojson", path, "--xy-crs", "4326"]
                       + list(extra))

    try:
        drift_path = tmpfile("drift.geojson", json.dumps(_stub_geojson()))
        before = read_bytes(drift_path)
        base = ["--from-geojson", drift_path, "--xy-crs", "4326"]
        with mock.patch.dict(sys.modules, {"arcpy": None}):
            code, out = run_cli(base)
        check(code == 1, "a file with drift exits 1, with arcpy made "
              "unimportable, so the mode never reaches for it")
        check("rows read: 7" in out and "OID 4 DRIFT" in out
              and "OID 7 DRIFT" in out and "OID 5 FILL" in out,
              "the file report names the same rows as the layer report")
        check("tolerance: 1e-06 degrees" in out,
              "a file defaults to the same 1e-6 degree tolerance")
        check("1 row(s) have no geometry" in out,
              "a feature with null geometry is left alone and counted")
        check(("source: %s, file modified " % printable(drift_path)) in out
              and "export time only if every copy kept it" in out
              and "not the live layer" in out,
              "the report names the file and its modified time, and says a "
              "copy resets that time  <-- pinned defect")
        check("A GeoJSON file is never written" in out
              and "resync 3 row(s)" in out,
              "the check says how many rows a resync would touch, and where "
              "a resync has to run")
        check(read_bytes(drift_path) == before
              and os.listdir(tmp) == ["drift.geojson"],
              "the run changed no byte of the file and wrote no other file")

        code, out = run_cli(base + ["--apply"])
        check(code == 64 and "--apply does not apply" in out,
              "--apply on a file is refused, because a file is never "
              "written  <-- pinned defect")
        check(read_bytes(drift_path) == before,
              "and the refused apply left the file as it was")
        code, out = run_cli(base + ["--workspace", "stub.gdb"])
        check(code == 64 and "--workspace does not apply" in out,
              "--workspace on a file is refused")
        code, out = run_cli(base + ["--wkid", "2237"])
        check(code == 64 and "--wkid does not apply" in out
              and "--xy-crs" in out,
              "--wkid on a file is refused and points at --xy-crs")
        code, out = run_cli(["--from-geojson", drift_path])
        check(code == 64 and "needs --xy-crs" in out,
              "a file run without --xy-crs is refused")
        absent = os.path.join(tmp, "absent.geojson")
        code, out = run_cli(["--from-geojson", absent, "--xy-crs", "2237"])
        check(code == 64 and "--xy-crs 2237" in out and "absent" not in out,
              "--xy-crs 2237 is refused before the file is even "
              "opened  <-- pinned defect")
        code, out = run_cli(base + ["--tolerance", "1e-9"])
        check(code == 64 and "rounding itself" in out,
              "a file tolerance under the grid is refused the same way")
        code, out = run_cli(base + ["--tolerance", "1", "--tolerance-units",
                                    "meters"])
        check(code == 1 and "tolerance: 8.98" in out,
              "a metre tolerance on a file is converted to degrees")
        code, out = run_cli(base + ["--tolerance", "100", "--tolerance-units",
                                    "meters"])
        check("  DRIFT             0" in out and "resync 1 row(s)" in out,
              "a 100 m tolerance hides the 11 m move in a file as well, and "
              "the empty row is still reported")
        code, out = run_cli(base + ["--tolerance", "0.001"])
        check(code == 1 and "tolerance: 0.001 degrees" in out,
              "a bare tolerance on a file is degrees")
        code, out = run_cli(base + ["--limit", "1"])
        check("... and 2 more" in out, "--limit reaches the file report")
        code, out = run_cli(base + ["--x-field", "LON"])
        check(code == 64 and "not in the properties" in out,
              "a misspelled column on a file is refused")
        code, out = run_cli(base + ["--x-field", "LABEL"])
        check(code == 64 and "finite number" in out,
              "a text column on a file is refused")

        code, out = run_file("clean.geojson",
                             json.dumps(_stub_geojson(_stub_rows()[:3])))
        check(code == 0 and "Every stored coordinate agrees" in out,
              "a clean file exits 0 and says so")
        code, out = run_file("antimeridian.geojson", json.dumps({
            "type": "FeatureCollection", "features": [
                {"type": "Feature", "id": 1, "geometry": {
                    "type": "Point", "coordinates": [180.0, -16.5]},
                 "properties": {"X": -180.0, "Y": -16.5}},
                {"type": "Feature", "id": 2, "geometry": {
                    "type": "Point", "coordinates": [-179.9999999, -16.6]},
                 "properties": {"X": 179.9999999, "Y": -16.6}}]}))
        check(code == 0 and "Every stored coordinate agrees" in out,
              "a file of points on the antimeridian runs clean end to end, so "
              "the file mode wraps longitude  <-- pinned defect")
        code, out = run_file("empty.geojson",
                             '{"type": "FeatureCollection", "features": []}')
        check(code == 64 and "no features" in out and "agrees" not in out,
              "an empty collection exits 64 and claims nothing agrees  "
              "<-- pinned defect")
        code, out = run_file("bom.geojson", b"\xef\xbb\xbf"
                             + json.dumps(_stub_geojson()).encode("utf-8"))
        check(code == 1 and "rows read: 7" in out,
              "a file saved with a byte order mark still reads")
        code, out = run_cli(["--from-geojson", absent, "--xy-crs", "4326"])
        check(code == 64 and "absent.geojson" in out,
              "a file that does not exist is refused by name")
        code, out = run_file("bad.geojson", "{")
        check(code == 64 and "error:" in out,
              "a file that is not JSON is refused")
        code, out = run_file("latin.geojson", b'{"a": "\xe9"}')
        check(code == 64 and "error:" in out,
              "a file that is not UTF-8 is refused")
        text = json.dumps(_stub_geojson())
        code, out = run_file("nan.geojson", text.replace("-82.4, ", "NaN, ", 1))
        check(code == 64 and "NaN" in out,
              "a NaN in the file is refused, not planned as "
              "clean  <-- pinned defect")
        code, out = run_file("inf.geojson",
                             text.replace("-82.4, ", "1e400, ", 1))
        check(code == 64 and "finite number" in out,
              "a number too large for a float is refused as well")
        code, out = run_file("infword.geojson",
                             text.replace("-82.4, ", "-Infinity, ", 1))
        check(code == 64 and "Infinity" in out, "so is Infinity")
        code, out = run_file("nanlabel.geojson",
                             text.replace('"p1"', "NaN", 1))
        check(code == 1 and "rows read: 7" in out,
              "a NaN in a column that is not compared does not stop the run")
        crs_doc = _stub_geojson()
        crs_doc["crs"] = {"type": "name",
                          "properties": {"name": "urn:ogc:def:crs:EPSG::2237"}}
        code, out = run_file("crs.geojson", json.dumps(crs_doc))
        check(code == 64 and "cannot reproject" in out,
              "a file that declares projected geometry is refused end to end")
        feet_doc = _stub_geojson()
        for feature in feet_doc["features"]:
            props = feature["properties"]
            props["X"] = None if props["X"] is None else props["X"] * -7000.0
            props["Y"] = None if props["Y"] is None else props["Y"] * 60000.0
        code, out = run_file("feet.geojson", json.dumps(feet_doc))
        check(code == 64 and "whatever --xy-crs says" in out,
              "columns in feet are refused even when --xy-crs claims 4326")

        refused_tolerances = []
        for bad in ("nan", "inf", "1e400"):
            code, out = run_cli(base + ["--tolerance", bad])
            refused_tolerances.append(code == 64 and "not a distance" in out)
        check(refused_tolerances == [True, True, True],
              "--tolerance nan, inf and 1e400 on a file are refused, not run "
              "as a clean file  <-- pinned defect")
        whole_turn = _stub_geojson(_stub_rows()[:3])
        whole_turn["features"][1]["properties"]["X"] = -82.2 - 7200.0
        whole_turn["features"][2]["properties"]["X"] = -82.3 + 360.0
        code, out = run_file("turn.geojson", json.dumps(whole_turn))
        check(code == 1 and "OID 2 DRIFT" in out and "OID 3 DRIFT" in out,
              "a stored X twenty turns off, or in the 0-360 convention, is "
              "drift in a file, not a match  <-- pinned defect")
        code, out = run_file("dup.geojson", (
            '{"type": "FeatureCollection", "features": [{"type": "Feature", '
            '"id": 1, "geometry": {"type": "Point", "coordinates": '
            '[-82.3999, 29.5]}, "properties": {"X": -82.4, "Y": 29.5, '
            '"X": -82.3999}}]}'))
        check(code == 64 and 'the name "X" appears twice' in out,
              "a file with one column named twice in a feature is refused, "
              "not read as whichever value won  <-- pinned defect")
        code, out = run_file("deep.geojson", "[" * 200000 + "]" * 200000)
        check(code == 64 and "error:" in out and "recursion" in out,
              "a file nested too deep to parse exits 64 with a message, not "
              "a traceback that exits 1  <-- pinned defect")
        with mock.patch("json.loads", side_effect=MemoryError):
            code, out = run_file("huge.geojson", json.dumps(_stub_geojson()))
        check(code == 64 and "error:" in out,
              "a file too large for memory exits 64, not a traceback that "
              "exits 1  <-- pinned defect")
        shapeless_doc = _stub_geojson(_stub_rows()[5:6] * 3)
        code, out = run_file("nogeom.geojson", json.dumps(shapeless_doc))
        check(code == 64 and "no feature has a geometry" in out
              and "Every stored" not in out,
              "a file with no geometry on any feature exits 64, not a clean "
              "0  <-- pinned defect")
        mostly = _stub_geojson(_stub_rows()[:1] + _stub_rows()[5:6] * 3)
        code, out = run_file("mostnull.geojson", json.dumps(mostly))
        check(code == 0 and "3 row(s) have no geometry" in out
              and "that has a geometry agrees" in out,
              "a file with one clean point and three without geometry exits "
              "0 and says three rows were not compared")
        forge = _stub_geojson(_stub_rows()[3:4])
        forge["features"][0]["id"] = ("4 OK\n\nEvery stored coordinate "
                                      "agrees with its geometry.\n\x1b[8m")
        code, out = run_file("forge.geojson", json.dumps(forge))
        check(code == 1 and "\x1b" not in out
              and "\nEvery stored coordinate agrees" not in out
              and "\\x0a\\x0aEvery stored" in out,
              "an id holding newlines and ESC[8m prints escaped, so it forges "
              "no clean line and hides no drift line  <-- pinned defect")
        forge = _stub_geojson(_stub_rows()[3:4])
        forge["features"][0]["id"] = "a\rb"
        forge["features"][0]["geometry"]["type"] = "Line\x1b[2K\rString"
        code, out = run_file("forgetype.geojson", json.dumps(forge))
        check(code == 64 and "feature a\\x0db has a Line\\x1b[2K\\x0dString"
              in out and "\r" not in out and "\x1b" not in out,
              "a refusal escapes the id and the geometry type it quotes from "
              "the file  <-- pinned defect")
        forge = _stub_geojson(_stub_rows()[3:4])
        forge["features"][0]["id"] = "a\nb"
        forge["features"][0]["properties"]["X"] = "-82.4"
        code, out = run_file("forgeprop.geojson", json.dumps(forge))
        check(code == 64 and "feature a\\x0ab" in out,
              "a refused column names its feature escaped as well")

        old_path = tmpfile("old.geojson", json.dumps(_stub_geojson()))
        os.utime(old_path, (0, -100000.0))
        code, out = run_cli(["--from-geojson", old_path, "--xy-crs", "4326"])
        check(code == 1 and "OID 4 DRIFT" in out,
              "a file modified before 1970 is still checked  <-- pinned defect")
        for exc in (OSError(22, "Invalid argument"),
                    OverflowError("timestamp out of range for platform time_t"),
                    ValueError("year is out of range")):
            with mock.patch("time.gmtime", side_effect=exc):
                code, out = run_cli(base)
            check(code == 1 and "file modified unknown" in out,
                  "a modified time the platform cannot convert (%s) prints as "
                  "unknown, and the check still runs"
                  % type(exc).__name__.lower())

        uni = _stub_geojson()
        uni["features"][3]["id"] = u"\u6771\u4eac"
        uni["features"][6]["id"] = u"\ud800"
        code, out = run_file(u"\u6771.geojson", json.dumps(uni))
        check(code == 1 and "OID \\u6771\\u4eac DRIFT" in out
              and "OID \\ud800 DRIFT" in out and "\\u6771.geojson" in out
              and is_ascii(out),
              "a CJK id, a lone surrogate id and a CJK file name print as "
              "ASCII escapes, so no console encoding can stop the "
              "report  <-- pinned defect")

        # ---- --infer-crs on a file
        def point_file(name, points):
            """A file of (id, x column, y column, lon, lat) points."""
            return tmpfile(name, json.dumps({
                "type": "FeatureCollection", "features": [
                    {"type": "Feature", "id": fid, "geometry": {
                        "type": "Point", "coordinates": [lon, lat]},
                     "properties": {"X": sx, "Y": sy}}
                    for fid, sx, sy, lon, lat in points]}))

        # The columns are PROJ 9.8.1's EPSG:3857 numbers, not this module's.
        proj_mercator = point_file("mercator.geojson", [
            (1, -9161594.092286414, 3426656.25037027, -82.3, 29.4),
            (2, -9139330.194127759, 3401126.26406649, -82.1, 29.2),
            (3, 1113194.9079327357, -8399737.889818357, 10.0, -60.0)])
        listing = sorted(os.listdir(tmp))
        before = read_bytes(proj_mercator)
        with mock.patch.dict(sys.modules, {"arcpy": None}):
            code, out = run_cli(["--from-geojson", proj_mercator,
                                 "--infer-crs"])
        check(code == 0 and "are in EPSG 3857." in out
              and "--layer and --wkid 3857" in out,
              "a file whose columns PROJ wrote in web mercator is named 3857, "
              "with arcpy made unimportable")
        check(read_bytes(proj_mercator) == before
              and sorted(os.listdir(tmp)) == listing,
              "--infer-crs on a file changed no byte of it and wrote no other "
              "file")
        code, out = run_cli(["--from-geojson", proj_mercator, "--xy-crs",
                             "4326"])
        check(code == 64 and "whatever --xy-crs says" in out,
              "the same file checked as 4326 is refused, which is the case "
              "--infer-crs exists for")
        code, out = run_cli(["--from-geojson", drift_path, "--infer-crs"])
        check(code == 0 and "rows read: 7" in out and "rows compared: 5" in out
              and "are in EPSG 4326." in out
              and "2 compared row(s) do not agree with EPSG 4326" in out
              and "Next: check the file for drift with --xy-crs 4326." in out,
              "the drift fixture is named 4326, and its two drifted rows are "
              "counted against it, not for another system")
        island_path = point_file("island.geojson", [
            (1, 0.0, 0.0, 0.0, 0.0), (2, -82.1, 29.2, -82.1, 29.2),
            (3, -82.2, 29.3, -82.2, 29.3)])
        code, out = run_cli(["--from-geojson", island_path, "--infer-crs"])
        check(code == 0 and "are in EPSG 4326." in out
              and "more than one candidate: 1" in out,
              "a file with a point at 0, 0 is named 4326, and the row that "
              "agrees with both is counted as shared  <-- pinned defect")
        mixed_path = point_file("mixed.geojson", [
            ("a", -82.1, 29.2, -82.1, 29.2), ("b", -82.2, 29.3, -82.2, 29.3),
            ("c", -82.3, 29.4, -82.3, 29.4),
            ("d", -9161594.092286414, 3426656.25037027, -82.3, 29.4),
            ("e", -9139330.194127759, 3401126.26406649, -82.1, 29.2)])
        code, out = run_cli(["--from-geojson", mixed_path, "--infer-crs"])
        check(code == 64 and "a mixed layer" in out
              and "rows that agree only with EPSG 3857: OID d, e" in out,
              "a file with rows in degrees and rows in web mercator is "
              "refused, and the web mercator rows are named by id  "
              "<-- pinned defect")
        code, out = run_cli(["--from-geojson", drift_path, "--infer-crs",
                             "4326,2237"])
        check(code == 64 and "can test only EPSG 4326 and 3857" in out
              and "Refused: EPSG 2237" in out,
              "a file refuses a candidate it cannot project into")
        code, out = run_cli(["--from-geojson", drift_path, "--infer-crs",
                             "--xy-crs", "4326"])
        check(code == 64 and "--xy-crs does not apply" in out,
              "--xy-crs with --infer-crs on a file is refused")
        code, out = run_cli(["--from-geojson", drift_path, "--infer-crs",
                             "--tolerance", "1e-9"])
        check(code == 64 and "rounding itself" in out,
              "a tolerance below the grid in any candidate is refused")
        code, out = run_cli(["--from-geojson", tmpfile("bad2.geojson", "{"),
                             "--infer-crs"])
        check(code == 64 and "bad2.geojson" in out,
              "a file that is not JSON is refused by name")
        code, out = run_cli(["--from-geojson", tmpfile(
            "crs2.geojson", json.dumps(crs_doc)), "--infer-crs"])
        check(code == 64 and "cannot reproject" in out,
              "a file that declares projected geometry is refused here too")
        code, out = run_cli(["--from-geojson", tmpfile(
            "nogeom2.geojson", json.dumps(shapeless_doc)), "--infer-crs"])
        check(code == 64 and "nothing was compared" in out,
              "a file with no geometry names no system")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    check(not os.path.exists(tmp), "the self-test removed its temp directory")

    # ---- which flags go together
    code, out = run_cli(["--layer", _StubArcpy.FC, "--from-geojson",
                         "x.geojson", "--xy-crs", "4326"])
    check(code == 64 and "not both" in out,
          "a layer and a file together are refused")
    same_stub = _StubArcpy(_stub_rows())
    code, out = run_stub(same_stub, y_field="X", apply=True)
    check(code == 64 and "both name X" in out and same_stub.updates == 0
          and same_stub.opened == [],
          "one column named as both X and Y is refused before --apply can "
          "write the latitude into it  <-- pinned defect")
    code, out = run_cli(["--from-geojson", "x.geojson", "--xy-crs", "4326",
                         "--x-field", "Y"])
    check(code == 64 and "both name Y" in out,
          "a file run refuses the same")
    code, out = run_cli(["--layer", _StubArcpy.FC, "--xy-crs", "4326"])
    check(code == 64 and "applies only to --from-geojson" in out
          and "--wkid" in out,
          "--xy-crs on a layer is refused and points at --wkid")

    # ---- arcpy itself, absent and present
    with mock.patch.dict(sys.modules, {"arcpy": None}):
        code, out = run_cli(["--layer", _StubArcpy.FC])
    check(code == 3 and "arcpy was not found" in out,
          "a layer run without arcpy exits 3, not the 1 that means drift "
          "found  <-- pinned defect")
    check(PRO_PYTHON in out and "--from-geojson" in out,
          "and names the Pro interpreter and the mode that needs none")
    class _Unlicensed(object):
        """A finder whose arcpy fails to start, as an unlicensed one does.

        It fails every import, and arcpy is the only one the run makes."""
        def find_spec(self, name, path=None, target=None):
            raise RuntimeError("NotInitialized")
    with mock.patch.dict(sys.modules):
        sys.modules.pop("arcpy", None)
        sys.meta_path.insert(0, _Unlicensed())
        try:
            code, out = run_cli(["--layer", _StubArcpy.FC])
        finally:
            sys.meta_path.pop(0)
    check(code == 3 and "NotInitialized" in out and "licence" in out,
          "an arcpy that fails to start without a licence exits 3, not a "
          "traceback that exits 1  <-- pinned defect")
    with mock.patch.dict(sys.modules, {"arcpy": _StubArcpy([])}):
        code, out = run_cli(["--layer", _StubArcpy.FC])
        typo, typo_out = run_cli(["--layer", _StubArcpy.FC, "--x-field", "TYPO"])
    check(code == 0 and typo == 64 and "column TYPO is not in" in typo_out,
          "an empty layer exits 0, because its field list still catches a "
          "misspelled column")
    with mock.patch.dict(sys.modules, {"arcpy": _StubArcpy(_stub_rows())}):
        code, out = run_cli(["--layer", _StubArcpy.FC])
    check(code == 1 and "resync 3 row(s)" in out,
          "the real entry point imports arcpy and runs the layer check")

    # ---- --infer-crs: the pure core
    origin = web_mercator(0.0, 0.0)
    check(origin[0] == 0.0 and abs(origin[1]) < 1e-9
          and rounded(origin[1]) == 0.0,
          "web mercator puts 0, 0 at its origin, to under a nanometre and "
          "exactly on the grid")
    check(web_mercator(180.0, 0.0)[0] == 20037508.342789244,
          "web mercator puts 180 degrees at 20037508.342789244 m, half the "
          "equator of the WGS84 sphere")
    north = web_mercator(-82.3, 29.4)
    south = web_mercator(10.0, -60.0)
    check(abs(north[0] - -9161594.092286414) < 1e-6
          and abs(north[1] - 3426656.25037027) < 1e-6
          and abs(south[0] - 1113194.9079327357) < 1e-6
          and abs(south[1] - -8399737.889818357) < 1e-6,
          "web mercator matches PROJ 9.8.1 to a micrometre, north and south "
          "of the equator")
    check(web_mercator(0.0, 90.0) == (None, None)
          and web_mercator(0.0, -90.0) == (None, None)
          and web_mercator(None, None) == (None, None),
          "a pole and a missing geometry have no web mercator position, not "
          "a traceback")

    sample = [(1, -82.1, 29.2, -82.1, 29.2),
              (2, -82.2, 29.3, -82.1999, 29.3),
              (3, None, 29.4, -82.3, 29.4),
              (4, nan, 29.5, -82.4, 29.5),
              (5, -82.5, 29.6, None, None),
              (6, -82.6, 29.7, nan, 29.7)]
    read, compared, agree = candidate_agreement(sample, tol, True)
    check(read == 6 and compared == {1, 2} and agree == {1},
          "a candidate compares only rows with a geometry and a number in "
          "both columns, and counts the ones that agree")
    check(candidate_agreement([(1, 0.0, 29.2, 0.0, 29.2001)], tol, True)[2]
          == set(), "a row agrees only when both of its columns agree")
    seam_row = [(1, 180.0, 10.0, -180.0, 10.0)]
    check(candidate_agreement(seam_row, tol, True)[2] == {1}
          and candidate_agreement(seam_row, tol, False)[2] == set(),
          "a geographic candidate wraps the antimeridian and a projected one "
          "does not")

    ten = set(range(1, 11))
    won = infer([(2237, ten, set(range(1, 10))), (4326, ten, set()),
                 (3857, ten, set())])
    check(won["winner"] == 2237 and won["kind"] == "winner"
          and won["reason"] is None,
          "the one candidate that explains 9 of 10 rows on its own is named")
    tie = infer([(4326, ten, set(ten)), (4269, ten, set(ten))])
    check(tie["winner"] is None and tie["kind"] == "tie"
          and "EPSG 4326, EPSG 4269 alike" in tie["reason"],
          "two candidates that agree with every row alike are a tie, not a "
          "win for the one listed first  <-- pinned defect")
    near = infer([(4326, ten, set(ten)), (4269, ten, set(range(1, 9)))])
    check(near["kind"] == "tie" and near["winner"] is None,
          "8 rows shared and 2 rows that only one candidate explains is "
          "still a tie, not a win on 2 rows  <-- pinned defect")
    half_shared = infer([(4326, ten, set(ten)),
                         (4269, ten, set(range(1, 6)))])
    check(half_shared["winner"] == 4326,
          "exactly half the rows shared is not a tie, so the tie edge is "
          "strict")
    island = infer([(4326, ten, set(ten)), (3857, ten, {1})])
    check(island["winner"] == 4326 and island["shared"] == {1},
          "a row at 0, 0, the same numbers in degrees and in web mercator "
          "metres, makes no tie  <-- pinned defect")
    mixed = infer([(4326, ten, set(range(1, 7))),
                   (2237, ten, set(range(7, 11))), (3857, ten, set())])
    check(mixed["kind"] == "mixed" and mixed["winner"] is None
          and "6 row(s) agree only with EPSG 4326; 4 row(s) agree only with "
          "EPSG 2237" in mixed["reason"],
          "6 rows in degrees and 4 in feet are a mixed layer, refused, not a "
          "win for 4326 by majority  <-- pinned defect")
    stray = infer([(4326, ten, set(range(1, 10))), (2237, ten, {10})])
    check(stray["kind"] == "mixed",
          "one row in another system is enough to make a layer mixed")
    check(infer([(4326, ten, set()), (3857, ten, set())])["kind"] == "none",
          "no candidate agreeing with any row is refused")
    minority = infer([(4326, ten, set(range(1, 6))), (3857, ten, set())])
    check(minority["kind"] == "minority"
          and "only 5 of 10" in minority["reason"],
          "a candidate that explains only half the compared rows is not "
          "named  <-- pinned defect")
    check(infer([(4326, ten, set(range(1, 7))), (3857, ten, set())])
          ["winner"] == 4326, "six of ten is a majority and is named")
    check(infer([(4326, set(), set()), (3857, set(), set())])["kind"]
          == "nothing", "a run that compared no row names nothing")
    check(infer([(3857, ten, set()), (4326, ten, set(ten))])["ranking"]
          == [4326, 3857]
          and infer([(3857, ten, set()), (2237, ten, set())])["ranking"]
          == [3857, 2237],
          "the ranking puts the explaining candidate first, and keeps the "
          "given order between candidates that explain nothing")

    mixed_text = "\n".join(describe_inference(
        mixed, 12, "0.5 meters", {2237: "datum transformation T"},
        {7: "a\nb"}, 2))
    check("rows read: 12" in mixed_text and "rows compared: 10" in mixed_text
          and "tolerance: 0.5 meters" in mixed_text,
          "the inference report counts the rows read and compared, and "
          "states the tolerance")
    check("EPSG 2237   agree      4   only this one      4   (datum "
          "transformation T)" in mixed_text,
          "the report names the transformation a candidate was read through")
    check("rows that agree only with EPSG 4326: OID 1, 2 ... and 4 more"
          in mixed_text
          and "rows that agree only with EPSG 2237: OID a\\x0ab, 8 ... and 2 "
          "more" in mixed_text and "only with EPSG 3857" not in mixed_text,
          "a mixed report names each system's rows, escaped and up to "
          "--limit, and skips a candidate that explains none")
    check("agree only" not in "\n".join(describe_inference(won, 10, "1 feet")),
          "a report that names a system lists no rows")

    # ---- --infer-crs on a layer, through the real entry point
    def infer_cli(stub, *extra):
        with mock.patch.dict(sys.modules, {"arcpy": stub}):
            return run_cli(["--layer", _StubArcpy.FC, "--infer-crs"]
                           + list(extra))

    feet = _infer_stub([2237] * 6)
    code, out = infer_cli(feet)
    check(code == 64 and "not on the list" in out
          and [w for _, w, _ in feet.opened] == [4326, 3857],
          "with no list a 4326 layer tries 4326 and 3857, and columns in "
          "feet agree with neither, so it refuses rather than guess")
    feet = _infer_stub([2237] * 6)
    code, out = infer_cli(feet, "4326,2237,3857")
    check(code == 0 and "result: the stored columns are in EPSG 2237." in out
          and "Next: check the layer for drift with --wkid 2237." in out,
          "columns in state plane feet on a lon/lat layer are named 2237")
    check(feet.updates == 0 and [f for f, _, w in feet.opened if w] == []
          and [w for _, w, _ in feet.opened] == [4326, 2237, 3857],
          "--infer-crs reads each candidate through its own cursor, and opens "
          "no writing cursor  <-- pinned defect")
    check("(datum transformation WGS_1984_(ITRF00)_To_NAD_1983)" in out,
          "a candidate on another datum is read through the transformation "
          "the check would use")

    degrees = _infer_stub([4326] * 5 + [None])
    code, out = infer_cli(degrees)
    check(code == 0 and "rows read: 6" in out and "rows compared: 5" in out
          and "are in EPSG 4326." in out and "do not agree" not in out,
          "lon/lat columns are named 4326, and an empty row is not compared")
    code, out = infer_cli(_infer_stub([3857] * 4))
    check(code == 0 and "are in EPSG 3857." in out,
          "web mercator columns are named 3857")
    drifted = _infer_stub([4326] * 4)
    drifted.rows[2]["X"] += 0.0001
    code, out = infer_cli(drifted)
    check(code == 0 and "are in EPSG 4326." in out
          and "1 compared row(s) do not agree with EPSG 4326" in out,
          "one drifted row does not stop the inference, and is counted")
    mixed_stub = _infer_stub([4326] * 3 + [2237] * 2)
    code, out = infer_cli(mixed_stub, "4326,2237")
    check(code == 64 and "a mixed layer" in out
          and "rows that agree only with EPSG 2237: OID 4, 5" in out,
          "a layer with rows in degrees and rows in feet is refused, and the "
          "rows in feet are named  <-- pinned defect")
    code, out = infer_cli(_infer_stub([4326] * 4), "4326,4269")
    check(code == 64 and "a tie" in out and "EPSG 4326, EPSG 4269" in out,
          "a NAD83 lon/lat candidate that reads the same as WGS84 here is "
          "refused as a tie  <-- pinned defect")
    own = _infer_stub([2237] * 3)
    own.layer_wkid = 2237
    code, out = infer_cli(own)
    check(code == 0 and "are in EPSG 2237." in out
          and [w for _, w, _ in own.opened] == [2237, 4326, 3857],
          "with no list a layer tries its own system first, then 4326 and "
          "3857")
    unknown = _infer_stub([4326] * 3)
    unknown.layer_wkid = 0
    code, out = infer_cli(unknown)
    check(code == 0 and [w for _, w, _ in unknown.opened] == [4326, 3857],
          "a layer in an unknown system, code 0, does not try 0")
    shapeless_infer = _infer_stub([4326] * 2)
    for row in shapeless_infer.rows.values():
        row["geom"] = None
    code, out = infer_cli(shapeless_infer)
    check(code == 64 and "nothing was compared" in out,
          "a layer with no geometry names no system")
    code, out = infer_cli(_infer_stub([4326] * 3), "--tolerance", "1",
                          "--tolerance-units", "feet")
    check(code == 0 and "tolerance: 1 feet" in out,
          "a foot tolerance is accepted and reported")

    refusals = []
    for extra, said in ((["--apply"], "only reads"),
                        (["--wkid", "2237"], "--infer-crs finds it"),
                        (["--xy-crs", "4326"], "--xy-crs does not apply"),
                        (["--tolerance-units", "degrees"], "meters or feet"),
                        (["4326"], "given only EPSG 4326"),
                        (["--y-field", "X"], "both name X")):
        stub = _infer_stub([4326] * 3)
        code, out = infer_cli(stub, *extra)
        refusals.append(code == 64 and said in out and stub.opened == []
                        and stub.updates == 0)
    check(refusals == [True] * 6,
          "--apply, --wkid, --xy-crs, degrees, a single candidate and one "
          "column named twice are refused before any cursor opens  "
          "<-- pinned defect")
    with mock.patch.dict(sys.modules, {"arcpy": _infer_stub([4326])}):
        code, out = run_cli(["--layer", "nowhere.gdb/P", "--infer-crs"])
    check(code == 64 and "does not exist" in out,
          "--infer-crs on a layer that does not exist is refused")
    code, out = infer_cli(_infer_stub([4326] * 3), "--x-field", "LON")
    check(code == 64 and "EPSG 4326: column LON is not in" in out,
          "a missing column is refused, naming the candidate")
    code, out = infer_cli(_infer_stub([4326] * 3), "--tolerance", "nan")
    check(code == 64 and "not a distance" in out,
          "a NaN tolerance is refused for --infer-crs as well")
    single_infer = _infer_stub([4326] * 3)
    single_infer.types = {"OBJECTID": "OID", "X": "Single", "Y": "Single"}
    code, out = infer_cli(single_infer)
    check(code == 64 and "is a Single" in out and single_infer.opened == [],
          "a Single lon/lat column too coarse for half a metre is refused "
          "before any cursor opens")
    code, out = infer_cli(_infer_stub([4326] * 3), "4326,9999")
    check(code == 3 and "reading stub.gdb/Points failed" in out,
          "a candidate the layer cannot be read in exits 3, not a traceback "
          "that exits 1")
    with mock.patch.dict(sys.modules, {"arcpy": None}):
        code, out = run_cli(["--layer", _StubArcpy.FC, "--infer-crs"])
    check(code == 3 and "arcpy was not found" in out,
          "--infer-crs on a layer without arcpy exits 3")

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
    prefix_refused = False
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            _parse(["--layer", "L", "--ap"])
        except SystemExit as exc:
            prefix_refused = exc.code == 64
    check(prefix_refused, "a unique prefix of --apply is refused, not read as "
          "--apply  <-- pinned defect")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        bad_limit = main(["--layer", "L", "--limit", "-1"])
        no_layer = main([])
    check(bad_limit == 64, "a negative --limit is a usage error")
    check(no_layer == 64, "no layer and no --self-test is a usage error")
    check("--layer is required" in err.getvalue(),
          "and the usage error says which flag is missing")

    check(_parse(["--from-geojson", "f", "--xy-crs", "4326"]).xy_crs == 4326,
          "--xy-crs is read as a number")
    usage = []
    for argv in (["--from-geojson", "f", "--xy-crs", "EPSG:4326"],
                 ["--layer", "L", "--tolerance", "abc"],
                 ["--layer", "L", "--furlongs"]):
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                main(argv)
        except SystemExit as exc:
            usage.append((exc.code, "error:" in err.getvalue()))
    check(usage == [(64, True)] * 3,
          "a value or a flag argparse rejects exits 64, not the 2 that means "
          "the resync failed part way  <-- pinned defect")
    check(a.from_geojson is None and a.xy_crs is None,
          "--from-geojson and --xy-crs default to none")
    check(a.infer_crs is None
          and _parse(["--layer", "L", "--infer-crs"]).infer_crs == []
          and _parse(["--layer", "L", "--infer-crs", "4326,2237"]).infer_crs
          == [4326, 2237]
          and _parse(["--layer", "L", "--infer-crs", " 4326,4326, 2237"])
          .infer_crs == [4326, 2237],
          "--infer-crs is off by default, empty for the defaults, and a list "
          "with each code once")
    bad_lists = []
    for argv in (["--layer", "L", "--infer-crs", "EPSG:4326"],
                 ["--layer", "L", "--infer-crs", "4326,"],
                 ["--layer", "L", "--infer-crs", "0,4326"],
                 ["--layer", "L", "--infer"]):
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                _parse(argv)
        except SystemExit as exc:
            bad_lists.append(exc.code)
    check(bad_lists == [64] * 4,
          "a code that is not a positive number, a trailing comma, and the "
          "prefix --infer are usage errors that exit 64")

    # ---- the stub's own guards, so a wrong test fails loudly
    stub = _StubArcpy(_stub_rows())
    raises(lambda: scan(stub, _StubArcpy.FC, "X", "Y",
                        stub.SpatialReference(3857), 1.0, False),
           "the stub refuses a system it holds no geometry for, rather than "
           "reading zeros", RuntimeError)
    with stub.da.SearchCursor(_StubArcpy.FC, ["OID@", "X", "Y"]) as cursor:
        raises(lambda: cursor.updateRow([1, 0.0, 0.0]),
               "the stub's search cursor refuses a write", RuntimeError)

    # ---- importing the module, which is how a snippet calls the cores.
    # Compiled and run by hand: an import through importlib would cache
    # bytecode in a __pycache__ beside the script, which is a write.
    here = os.path.dirname(os.path.abspath(__file__))
    beside = sorted(os.listdir(here))
    probe = ModuleType("xydrift_probe")
    probe.__file__ = __file__
    with open(__file__, "rb") as handle:
        code_obj = compile(handle.read(), __file__, "exec")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(code_obj, probe.__dict__)
    check(out.getvalue() == "" and callable(probe.plan_rows)
          and callable(probe.geojson_rows),
          "importing the module runs nothing and exposes the cores")
    check(sorted(os.listdir(here)) == beside,
          "and writes nothing beside the script, not even a "
          "__pycache__  <-- pinned defect")

    # ---- the harness itself, which must be able to fail
    p_check, p_raises, p_passed, p_failed = _harness()
    with contextlib.redirect_stdout(io.StringIO()):
        p_check(True, "t")
        p_check(False, "f")
        p_raises(lambda: None, "n")
        p_raises(lambda: 1 / 0, "z")
        p_raises(lambda: int("x"), "v")
    check(p_passed[0] == 2 and p_failed[:2] == ["f", "n (no error raised)"],
          "a false check and a raises() that raised nothing both count as "
          "failures")
    check(len(p_failed) == 3
          and p_failed[2].startswith("z (wrong exception ZeroDivisionError"),
          "an exception of the wrong type is a failure, not a pass")
    check(_footer(2, ["f"]) == (["3 assertions, 1 failed", "  FAILED: f"], 1),
          "a run with a failure prints it and exits 1")
    check(_footer(3, []) == (["3 assertions, 0 failed"], 0),
          "a green run prints the count and exits 0")

    print("-" * 68)
    lines, code = _footer(passed[0], failed)
    for line in lines:
        print(line)
    return code


# ----------------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2 on a usage error, and 2 here means the resync
        # failed part way. A typo must not look like a half-written layer.
        self.print_usage(sys.stderr)
        self.exit(64, "%s: error: %s\n" % (self.prog, message))


def _parse(argv):
    ap = _Parser(
        prog="xydrift.py",
        description="Name the rows whose stored X and Y columns disagree with "
                    "their own geometry, and resync only those.",
        epilog="Nothing is written without --apply.",
        allow_abbrev=False,
    )
    ap.add_argument("--layer", help="point feature class to check. Needs arcpy.")
    ap.add_argument("--from-geojson", dest="from_geojson", metavar="FILE",
                    help="check a GeoJSON file of points instead of a layer. "
                         "Read-only, needs no arcpy, and needs --xy-crs.")
    ap.add_argument("--xy-crs", dest="xy_crs", type=int, metavar="EPSG",
                    help="EPSG code the stored columns in a GeoJSON file are "
                         "in. Only %d is accepted, because GeoJSON geometry "
                         "is always lon/lat and nothing here reprojects."
                         % GEOJSON_WKID)
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
    ap.add_argument("--infer-crs", dest="infer_crs", nargs="?", const="",
                    type=wkid_list, metavar="EPSG,...",
                    help="rank the systems the stored columns could be in, "
                         "and name one or refuse on a tie or a mixed layer. "
                         "Read-only. With no list a layer tries its own "
                         "system, %d and %d, and a file tries %d and %d."
                         % (GEOJSON_WKID, WEB_MERCATOR_WKID, GEOJSON_WKID,
                            WEB_MERCATOR_WKID))
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if args.layer and args.from_geojson:
        print("error: give --layer or --from-geojson, not both.",
              file=sys.stderr)
        return 64
    if not (args.layer or args.from_geojson):
        print("error: --layer is required, or --from-geojson with --xy-crs "
              "for a file. Use --self-test to verify the tool without a "
              "geodatabase.", file=sys.stderr)
        return 64
    if args.limit < 0:
        print("error: --limit cannot be negative.", file=sys.stderr)
        return 64

    if args.infer_crs is not None:
        return run_infer(args)
    if args.from_geojson:
        return run_geojson(args)
    if args.xy_crs is not None:
        print("error: --xy-crs applies only to --from-geojson. A layer's "
              "columns are read in --wkid.", file=sys.stderr)
        return 64
    arcpy = _import_arcpy()
    if arcpy is None:
        return 3
    return run(args, arcpy)


if __name__ == "__main__":
    sys.exit(main())
