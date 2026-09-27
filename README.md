# xydrift

Name the rows whose stored X and Y columns disagree with their own geometry, and resync only
those.

An address layer carries X and Y columns beside its real point geometry. An editor drags a
handful of points onto the correct driveways on a Thursday afternoon. The geometry moves. The two
columns do not, because nothing in a geodatabase links an attribute to a shape.

Nothing reports an error. The map is right. The table is right on every row an editor looked at,
and the rows that moved still hold the coordinate they used to have. A week later a report built
from the columns places those addresses in the wrong block, and no log anywhere says when that
started.

The usual repair makes a second problem. Calculate Geometry Attributes recomputes both columns on
every row in the layer, so `last_edited_date` and `last_edited_user` are stamped on all of them.
The record of who moved what is gone, and it was the only way to answer the question the wrong
report just raised. An attribute rule is the better long-term answer. It repairs none of this,
because a rule fires on edit and the drifted rows are not being edited.

This reads the layer, names the rows that actually disagree, and rewrites only those. It can also
check a GeoJSON export of the layer on a machine with no ArcGIS software. That mode only reads.

```
$ python xydrift.py --self-test
xydrift self-test: no arcpy, no database, no network
--------------------------------------------------------------------
PASS  an identical coordinate is 0 apart
PASS  a plain distance is the same in both directions
PASS  -180 and +180 are the same meridian, not 360 degrees of drift  <-- pinned defect
...
PASS  a stored X 1e-7 past 180 is inside the tolerance of the meridian, so it still wraps  <-- pinned defect
PASS  the same stored X past the seam is OK against -180 and +180 alike, and measures 1e-7 on both  <-- pinned defect
PASS  --apply leaves that row alone, so it stamps no editor tracking  <-- pinned defect
...
PASS  a difference of exactly the tolerance is inside it  <-- pinned defect
PASS  one ulp above the tolerance is drift
PASS  one ulp below the tolerance is not drift
...
PASS  only the column that drifted is rewritten  <-- pinned defect
...
PASS  half a metre against a lon/lat column converts to degrees  <-- pinned defect
...
PASS  a text coordinate column is refused, not compared  <-- pinned defect
...
PASS  a Single easting cannot hold a hundredth of a foot at state plane magnitudes  <-- pinned defect
...
PASS  a clean report still counts the rows it could not compare  <-- pinned defect
...
PASS  rows that all lack a geometry are reported as not compared, not as clean  <-- pinned defect
...
PASS  newline, CR, tab, ESC and DEL in an id print escaped, so an id cannot forge or hide a report line  <-- pinned defect
...
PASS  a projected --wkid reports the largest corner of the extent in that system, not the layer's own lon/lat corners  <-- pinned defect
...
PASS  the scan rounds the geometry, so noise under the grid is not drift  <-- pinned defect
...
PASS  a row that moved north has its Y rewritten and its X left alone  <-- pinned defect
...
PASS  after the abort the layer holds what it held before  <-- pinned defect
...
PASS  a 100 m tolerance hides the 11 m move, which is the other way a tolerance ruins this  <-- pinned defect
...
PASS  a Single column is refused from the extent in --wkid, on a lon/lat layer whose columns hold state plane feet  <-- pinned defect
...
PASS  a NaN column read from a layer is drift and exits 1, not a clean layer  <-- pinned defect
...
PASS  a layer whose only point is NaN compared nothing, and exits 64, not 0  <-- pinned defect
...
PASS  the core calls a NaN column drift, not clean, so no reader has to remember to refuse it  <-- pinned defect
...
PASS  stored columns in state plane are refused, not compared with lon/lat geometry  <-- pinned defect
...
PASS  NAD83 and NAD27 lon/lat are refused too, because they are not the WGS84 the geometry is in and nothing here shifts a datum  <-- pinned defect
...
PASS  a file that declares projected geometry is refused  <-- pinned defect
...
PASS  a longitude past 180, the 0-360 convention, is refused  <-- pinned defect
PASS  a NaN geometry is refused, not planned as no geometry  <-- pinned defect
PASS  a NaN latitude alone is refused as well  <-- pinned defect
...
PASS  a NAD27 crs on the feature is refused, not compared with WGS84 columns as clean  <-- pinned defect
PASS  a CRS84 crs on the feature is lon/lat and is read
PASS  a NAD27 crs on the geometry is refused, not compared with WGS84 columns as clean  <-- pinned defect
...
PASS  columns in feet under --xy-crs 4326 are refused, because the caller's word was wrong  <-- pinned defect
...
PASS  a file and a layer holding the same rows plan identically, down to the measured drift
...
PASS  a file with drift exits 1, with arcpy made unimportable, so the mode never reaches for it
...
PASS  the report names the file and its modified time, and says a copy resets that time  <-- pinned defect
...
PASS  the run changed no byte of the file and wrote no other file
PASS  --apply on a file is refused, because a file is never written  <-- pinned defect
...
PASS  a file of points on the antimeridian runs clean end to end, so the file mode wraps longitude  <-- pinned defect
PASS  an empty collection exits 64 and claims nothing agrees  <-- pinned defect
...
PASS  a NaN in the file is refused, not planned as clean  <-- pinned defect
...
PASS  a file too large for memory exits 64, not a traceback that exits 1  <-- pinned defect
PASS  a file with no geometry on any feature exits 64, not a clean 0  <-- pinned defect
...
PASS  an id holding newlines and ESC[8m prints escaped, so it forges no clean line and hides no drift line  <-- pinned defect
...
PASS  a file modified before 1970 is still checked  <-- pinned defect
...
PASS  one column named as both X and Y is refused before --apply can write the latitude into it  <-- pinned defect
...
PASS  an arcpy that fails to start without a licence exits 3, not a traceback that exits 1  <-- pinned defect
...
PASS  the real entry point imports arcpy and runs the layer check
...
PASS  an exception of the wrong type is a failure, not a pass
PASS  a run with a failure prints it and exits 1
PASS  a green run prints the count and exits 0
--------------------------------------------------------------------
373 assertions, 0 failed
```

## Requirements

Python 3.9 or later. Nothing to install. The tool uses the standard library only: `argparse`,
`json`, `math`, `os`, `sys` and `time`. The self-test adds `contextlib`, `io`, `shutil`,
`tempfile`, `types` and `unittest.mock`.

Only one mode needs `arcpy`:

| Mode | Needs `arcpy` | Writes |
|---|---|---|
| `--layer` | Yes. Run it with the Python that ships with ArcGIS Pro. | Only with `--apply`. |
| `--from-geojson` | No. Runs on Linux and Windows. | Never. |
| `--self-test` | No. | Only a temp directory, which it deletes again. |

The self-test needs no `arcpy`, no network, no credentials and no geodatabase. It prints the same
373 assertions, line for line, on Windows with Python 3.13, on Linux with Python 3.12, and on
Windows with Python 3.9.

```
git clone https://github.com/uhsear/xydrift.git
python xydrift.py --self-test
```

## Usage

The check is read-only. Nothing is written without `--apply`, and a GeoJSON file is never
written at all.

```
python xydrift.py --layer demo.gdb/Sensors
python xydrift.py --layer demo.gdb/Sensors --tolerance 0.5 --tolerance-units meters
python xydrift.py --layer demo.gdb/Sensors --x-field PT_X --y-field PT_Y
python xydrift.py --layer prod.sde/Sensors --workspace prod.sde --apply
python xydrift.py --from-geojson sensors.geojson --xy-crs 4326
```

A real run under ArcGIS Pro 3.6 against a seven row layer. Two points were moved about 11 m
without their columns, one row was never populated, and one row has no geometry.

The seven rows are synthetic. They are the self-test's own fixture, so the numbers below also
appear in the self-test, and you can rebuild the same layer. Write the fixture to a file, then
load it into a file geodatabase with the Python that ships with ArcGIS Pro:

```
python -c "import xydrift, json; json.dump(xydrift._stub_geojson(), open('sensors.geojson', 'w'))"
```

```python
import json, arcpy
arcpy.management.CreateFileGDB(".", "demo.gdb")
arcpy.management.CreateFeatureclass("demo.gdb", "Sensors", "POINT",
                                    spatial_reference=arcpy.SpatialReference(4326))
for name in ("X", "Y"):
    arcpy.management.AddField("demo.gdb/Sensors", name, "DOUBLE")
with arcpy.da.InsertCursor("demo.gdb/Sensors", ["SHAPE@XY", "X", "Y"]) as cursor:
    for f in json.load(open("sensors.geojson"))["features"]:
        xy = tuple(f["geometry"]["coordinates"]) if f["geometry"] else None
        cursor.insertRow([xy, f["properties"]["X"], f["properties"]["Y"]])
```

```
$ python xydrift.py --layer demo.gdb/Sensors
rows read: 7
tolerance: 1e-06 degrees
  NO_GEOMETRY       1
  DRIFT             2
  FILL              1
  OK                3

rows whose columns disagree with their geometry:
  OID 4 DRIFT: dx=0.0001 dy=0
  OID 5 FILL: dx=none dy=none
  OID 7 DRIFT: dx=0 dy=0.0001

1 row(s) have no geometry. Their stored coordinates are left alone.

Check only. Nothing was written. Re-run with --apply to resync 3 row(s).
```

The same layer with `--apply`, then checked again:

```
$ python xydrift.py --layer demo.gdb/Sensors --workspace demo.gdb --apply
...
=== APPLY ===
resynced 3 row(s).

$ python xydrift.py --layer demo.gdb/Sensors
rows read: 7
tolerance: 1e-06 degrees
  NO_GEOMETRY       1
  DRIFT             0
  FILL              0
  OK                6

Every stored coordinate that has a geometry agrees with it.

1 row(s) have no geometry. Their stored coordinates are left alone.
```

The report says "that has a geometry" because one row was not compared. When no row has a
geometry at all, the run compared nothing, so it says that and exits 64 instead of 0.

### A GeoJSON file, with no ArcGIS software

The same seven points, exported from the demo layer before the resync by the ArcGIS Pro 3.6
Features To JSON tool with its GeoJSON option. The file was copied with `scp -p` to a Linux server
and checked there with Python 3.12 and no `arcpy`. The rows and the measured drift are the same
as the layer run.

```
$ python3 xydrift.py --from-geojson sensors.geojson --xy-crs 4326
source: sensors.geojson, file modified 2026-09-26 21:47:21 UTC. That is the export time only if every copy kept it. A file is a snapshot, not the live layer.
rows read: 7
tolerance: 1e-06 degrees
  NO_GEOMETRY       1
  DRIFT             2
  FILL              1
  OK                3

rows whose columns disagree with their geometry:
  OID 4 DRIFT: dx=0.0001 dy=0
  OID 5 FILL: dx=none dy=none
  OID 7 DRIFT: dx=0 dy=0.0001

1 row(s) have no geometry. Their stored coordinates are left alone.

Check only. A GeoJSON file is never written. To resync 3 row(s), run --layer on the layer itself with --apply under ArcGIS Pro.
```

The time on the `source:` line is the file's own modified time. `scp -p` and `cp -p` keep it. A
plain `scp`, `cp`, download or `git checkout` sets it to the time of the copy. The same bytes,
copied with a plain `scp` 17 seconds later, reported `file modified 2026-09-26 21:47:38 UTC`.

GeoJSON geometry is always WGS84 longitude and latitude. The stored columns can be compared with
it only if they are in that system too, and this mode cannot reproject. So you must state the
system of the columns, and any answer except 4326 is refused before the file is opened. That
includes NAD83 (4269) and NAD27 (4267), which are longitude and latitude on another datum:

```
$ python3 xydrift.py --from-geojson sensors.geojson --xy-crs 2237
error: --xy-crs 2237: GeoJSON geometry is always WGS84 lon/lat (EPSG:4326), and columns in any other system, another lon/lat datum included, would be compared in the wrong frame. This mode cannot reproject, so it will not compare them. Export the columns in EPSG:4326, or run --layer with --wkid 2237 under ArcGIS Pro.
```

| Flag | Default | What it does |
|---|---|---|
| `--layer` | none | Point feature class to check. Needs `arcpy`. |
| `--from-geojson` | none | GeoJSON file of points to check instead of a layer. Needs no `arcpy`. |
| `--xy-crs` | none | EPSG code of the stored columns in a GeoJSON file. Required with `--from-geojson`. Only `4326` is accepted. |
| `--x-field` | `X` | Stored longitude or easting column. |
| `--y-field` | `Y` | Stored latitude or northing column. |
| `--tolerance` | `1e-06` degrees | Largest difference that is not drift. Required for a projected layer. |
| `--tolerance-units` | the layer's own | `degrees`, `meters` or `feet`. |
| `--wkid` | `4326` | Coordinate system the stored columns of a layer are in. |
| `--workspace` | none | Geodatabase to open an edit session on. Needed for a versioned class. |
| `--limit` | `10` | Rows listed before the report counts the rest. |
| `--apply` | off | Write the resynced coordinates to a layer. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

Give `--layer` or `--from-geojson`, not both. `--apply`, `--workspace` and a `--wkid` other than
4326 are refused with `--from-geojson`, and `--xy-crs` is refused with `--layer`. In both modes,
`--x-field` and `--y-field` must name two different columns. With one column named twice,
`--apply` would write the latitude over the longitude.

Exit codes: 0 no drift or the resync finished, 1 drift found and not written, 2 the resync failed
part way, 3 `arcpy` is missing, cannot start without a licence, or failed while reading and
nothing was written, 64 usage error, refused input, or no row had a geometry to compare.
`--from-geojson` never exits 2 or 3, because it never writes and never imports `arcpy`. A `--layer` run without `arcpy` prints the Pro interpreter path and exits 3, not 1, so a
scheduled check under the wrong Python does not report drift every night.

## What it checks

- **Each column on its own.** A row whose Y moved and whose X did not has only its Y rewritten.
  Rewriting the column that already agreed is an edit nobody asked for, and it stamps editor
  tracking exactly as a blanket recalculation does.
- **The tolerance, at both ends.** The comparison is strictly greater than, so a difference of
  exactly the tolerance is inside it. One ulp either side of that edge is pinned. `1e-6` degrees
  is about 0.11 m, which is below any real edit and above the arithmetic.
- **Rounding, before comparing.** The geometry token returns a full float64, whose last bits
  differ from the number that was stored. Both sides are rounded to 8 decimals first. The
  self-test asserts that this rounding never manufactures drift at the default tolerance.
- **A tolerance that is too fine to mean anything.** Eight decimals is a grid of `1e-8` degrees.
  A tolerance at or below that grid is refused, because the rounding alone would then read as
  drift on most rows. The geometry is rounded in whatever system it is read in, so the same
  floor applies to a projected layer in feet, after any unit conversion rather than before.
- **Units.** A tolerance of `0.5` against a lon/lat column is half a degree, about 55 km. Passing
  `--tolerance-units meters` converts it to `4.5e-06` degrees instead. A degree tolerance against
  a projected layer is refused rather than converted.
- **The antimeridian.** -180 and +180 are the same meridian. Subtracting them gives 360, the
  largest drift a lon/lat layer can hold, on a row where nothing moved. Only that seam wraps. A
  stored X no further past +/-180 than the tolerance is on the meridian, so `180.0000001` is OK
  against a geometry at -180 as well as at +180. A stored X further past +/-180, such as
  `-7282.2` or `277.8` in the 0-360 convention, is not a longitude. It is reported as drift,
  not matched a whole turn away. A gap of 180.5 degrees measures 179.5, so the fold is at 180.
- **The column type.** A String coordinate column is refused before any cursor opens, because
  "-82.1" and "-82.10" are the same place and two different strings. A Single column is refused
  when its own step is coarser than the tolerance. Near longitude 82 a Single resolves to
  `7.6e-06` degrees, and near 180 to `1.5e-05`. Every row would then read as drifted for ever.
- **The column type on a projected layer**, where the same check needs a number the tolerance
  cannot supply. A Single's step grows with the coordinate, so the layer's extent is read first,
  projected into `--wkid`. A state plane easting of 600,000 ft resolves to `0.0625` ft, and a
  northing of 1,800,000 ft to `0.125` ft, both coarser than any tolerance an editor would set.
  The extent is stored metadata, so reading it costs no extra pass over the rows. It is projected
  because Describe reports it in the layer's own system. A lon/lat layer whose Single columns
  hold state plane feet read as magnitude 82 and passed the check, and every row read as drift.
  Under ArcGIS Pro 3.6, that layer is now refused at a tolerance of 0.01 ft, and it runs clean at
  0.2 ft.
- **A row with no geometry.** Its stored coordinates are left exactly as they are. A
  recalculation that starts from the geometry nulls them, and that row then has no coordinate at
  all. The report counts these rows whether or not anything drifted. A geometry coordinate that
  reads as NaN counts as no geometry, not as a clean row.
- **A run that compared nothing.** When every row lacks a geometry, the report says that no
  coordinate was compared, and the run exits 64. A table exported without its shapes would
  otherwise pass a scheduled check every night. A GeoJSON file with no features also exits 64.
  A file has no schema, so an empty one cannot show that the column names are spelled right. An
  empty layer still exits 0, because its field list confirms the names.
- **NaN in a column.** A NaN minus anything is NaN, and NaN is never greater than a tolerance. The
  comparison is therefore written as "within the tolerance, or drift", so a NaN column is DRIFT
  and a resync writes the geometry's value over it. The check is in the shared core, so the layer
  scan and the file reader both get it.
- **The writing cursor.** The update cursor opens on the OID and the two columns, and never on a
  geometry token. The geometry is read by a separate cursor. A rounded geometry written back
  would move the point this tool exists to trust.

### In a GeoJSON file

The file mode feeds the same decision functions as the layer scan, with longitude wrapping on.
The self-test builds one set of seven rows as a layer and as a file, and asserts that both give
the same verdicts, measured drift and planned values.

- **The system of the columns.** `--xy-crs` is required, and any value except 4326 is refused
  before the file is opened. A state plane or web mercator column compared with lon/lat geometry
  reports every row as drift.
- **The system of the geometry.** RFC 7946 removed the `crs` member, but older files still carry
  one, on the collection or, as the 2008 spec allowed, on a feature or a geometry. A file that
  names anything except CRS84 or EPSG:4326 in any of those places is refused. A file that GDAL
  3.12.4 reprojected to EPSG:2237 was refused on this check. The same points written by GDAL with
  its CRS84 name, and with its RFC 7946 option, both read.
- **Geometry that is not lon/lat.** A point beyond +/-180 or +/-90 is refused, because the file
  was written in another system. A writer's `180.00000000001` is rounded to the grid first, so it
  still counts as the meridian.
- **Columns that plainly are not degrees.** `--xy-crs` is your word, and the file can contradict
  it. When no stored value in a column is within +/-180 (X) or +/-90 (Y), the run is refused.
  One wild value among real longitudes is only a drifted row, and is reported as that.
- **NaN and Infinity.** Python reads both from a file, although JSON has neither. A NaN, an
  infinity or a number too large for a float in either column or the geometry is refused, so a
  file that holds one is fixed at its source rather than reported as drift. A NaN in a column
  that is not compared does not stop the run.
- **Text and true or false.** GeoJSON has no column types, so each value is checked. A coordinate
  stored as `"-82.1"` or `true` is refused, and the error names the column and the feature.
- **Absent and misspelled columns.** A property that is absent from one feature reads as null, so
  that row plans as FILL, the same as a null in a layer. A column that no feature carries is
  refused as a misspelling.
- **Geometry types.** Only points are compared. A null geometry or an empty point is
  NO_GEOMETRY. Any other geometry type, such as a line or a multipoint, is refused.
- **Text from the file.** A feature `id` or a geometry type can hold a newline or an escape
  sequence. Printed raw, a newline forges a report line and `ESC[8m` hides the real one. Every
  value taken from the file is printed with control characters and non-ASCII characters escaped,
  in the report and in every error message.
- **Nothing is written.** The self-test runs the mode with `arcpy` made unimportable, then
  asserts that no byte of the file changed and no other file appeared. `--apply` and
  `--workspace` are refused.

## Why not Calculate Geometry Attributes

Calculate Geometry Attributes is the right tool when you want every row rewritten, and it is
faster than a cursor. It has no comparison in it. It writes all rows, so it stamps editor
tracking on all rows, and it reports nothing: you cannot ask it which rows were wrong.

That report is most of the value here. "Three rows drifted" and "every row recalculated" answer
different questions, and only the first one tells you an editor moved something last Thursday.

An attribute rule that maintains the columns on insert and update is better than either. It
fixes nothing that is already wrong, because it fires on edit. Use this to repair the layer, then
add the rule so it stays repaired.

For a file, a GDAL query in the SQLite dialect makes the same comparison with no script at all:

```
SELECT X, Y, ST_X(geometry), ST_Y(geometry) FROM sensors
WHERE abs(ST_X(geometry) - X) > 1e-6 OR abs(ST_Y(geometry) - Y) > 1e-6
   OR X IS NULL OR Y IS NULL
```

Run through GDAL 3.12.4 on the seven points above, it flags the same three rows, and it correctly
ignores the row with no geometry. It is the quicker answer for a one-off check. On two points that
sit on the antimeridian it measures 360 degrees of drift for each, where this tool reports both as
clean. On the same seven points reprojected to state plane feet, it flags all six rows that have a
geometry, and nothing says why. This tool refuses that file and names the reason.

## Limits

- It never moves a point. It only writes the two attribute columns, and only on the rows that
  disagree.
- `--wkid` has to match the coordinate system the columns are stored in. Reading lon/lat columns
  against projected geometry reports every row as drifted by millions of feet, correctly and
  uselessly.
- The metre and foot conversions for a geographic layer use 111320 m per degree, the equatorial
  figure. A degree of longitude shrinks toward the poles, so the converted tolerance is the
  tightest one, never a wider one. It never hides a real move, and the further a layer sits from
  the equator the more ordinary noise it can report as drift.
- Every row is read. There is no where clause and no way to check one part of a layer.
- The plans are held in memory. A few hundred thousand rows are fine; tens of millions are not.
- The plan is built from one read and written by a second cursor. A row edited between the two is
  written with the coordinate read in the first pass. Use `--workspace` on a versioned class, and
  run it when nobody is editing.
- It has no opinion about which value is right. The geometry always wins, because the geometry is
  what the map draws.
- A Single coordinate column is refused rather than compared loosely. Widen the tolerance past
  the column's own step if you want it checked anyway.
- A GeoJSON file is a snapshot, not the live layer. The report prints the file's modified time,
  which is the time of the export only if every copy kept it. A plain copy resets it, so a year-old
  export copied this morning reports this morning. When the operating system cannot convert the
  time, as Windows cannot for a time before 1970, the report prints `unknown` and still checks the
  file. To fix the rows it names, run `--layer` with `--apply` under ArcGIS Pro.
- The file mode compares only in EPSG:4326. It cannot reproject, so columns in any other system
  are refused rather than converted.
- A `crs` member that names EPSG:4326 is read as longitude first, which is the order RFC 7946
  fixes. A file that really holds latitude first reports every row as drift.
- The check on columns that are plainly not degrees is a guard, not a proof. Columns in a
  projected system whose values all happen to fall within +/-180 would pass it.
- GeoJSON numbers have no Single type, so the Single check does not apply to a file.
- The OID in a file report is the feature's `id` member, including an `id` of 0. When a feature
  has none, it is the feature's 1-based position in the file.
- The whole file is parsed into memory, like the plans.
- Branch coverage of the self-test is 100 percent, measured with coverage.py 7.16.1 on
  Python 3.13. The self-test imports `arcpy` through a stub, runs the file mode with `arcpy` made
  unimportable, re-imports its own module, and makes its own harness fail on purpose, so no line
  is left unexercised.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [geocodesift](https://github.com/uhsear/geocodesift) - also does coordinate arithmetic, and
  asks a different question: whether a geocoder's own answer is believable. It reads a CSV, never
  a feature class. It compares a coordinate with a match type and a boundary, never with the
  geometry the row is stored on.
- [roadmiles](https://github.com/uhsear/roadmiles) - measures geodesic length and finds the same
  line digitized twice. Its arithmetic is between two geometries. This one compares a geometry
  with an attribute, which is the pair no geometry operation can see.
- [fcpatch](https://github.com/uhsear/fcpatch) - applies reviewed attribute edits, and refuses
  geometry columns by design. It is the tool for the rows this one names when the fix is a value
  somebody chose.
- [arcpy-nullscan](https://github.com/uhsear/arcpy-nullscan) - the NULLs the same table also
  carries.
- [tzrot](https://github.com/uhsear/tzrot) - the same species of silent column corruption, in a
  date field.
