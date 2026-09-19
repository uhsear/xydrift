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

This reads the layer, names the rows that actually disagree, and rewrites only those.

```
$ python xydrift.py --self-test
xydrift self-test: no arcpy, no database, no network
--------------------------------------------------------------------
...
PASS  -180 and +180 are the same meridian, not 360 degrees of drift  <-- pinned defect
PASS  two points either side of the antimeridian are 1e-6 degrees apart
...
PASS  a difference of exactly the tolerance is inside it  <-- pinned defect
PASS  one ulp above the tolerance is drift
PASS  one ulp below the tolerance is not drift
...
PASS  rounding the geometry to 8 decimals never manufactures drift at 1e-6  <-- pinned defect
...
PASS  only the column that drifted is rewritten  <-- pinned defect
...
PASS  half a metre against a lon/lat column converts to degrees  <-- pinned defect
PASS  the converted tolerance is small, not the 0.5 degrees a passthrough would have accepted
...
PASS  a projected tolerance under the grid is refused as well, because the geometry is rounded in feet too  <-- pinned defect
...
PASS  a text coordinate column is refused, not compared  <-- pinned defect
...
PASS  a Single easting cannot hold a hundredth of a foot at state plane magnitudes  <-- pinned defect
PASS  the same column is fine at a one foot tolerance
...
PASS  a state plane extent reports its largest coordinate, not its easting  <-- pinned defect
...
PASS  the scan rounds the geometry, so noise under the grid is not drift  <-- pinned defect
PASS  the same pair compared unrounded is drift, so the rounding in the scan is what did the work
...
PASS  a row that moved north has its Y rewritten and its X left alone  <-- pinned defect
PASS  the empty row is filled from its geometry
...
PASS  and opens no update cursor at all, rather than opening one and finding nothing to do  <-- pinned defect
...
PASS  after the abort the layer holds what it held before  <-- pinned defect
PASS  a refused write without an edit session is raised too
...
PASS  a 100 m tolerance hides the 11 m move, which is the other way a tolerance ruins this  <-- pinned defect
...
PASS  a Single column on a projected layer is refused from the layer's own extent  <-- pinned defect
PASS  and that refusal also happens before any cursor opens
...
--------------------------------------------------------------------
181 assertions, 0 failed
```

## Requirements

Python 3.9 or later. The self-test uses the standard library only: `argparse`, `math`, `sys`, and
`io` and `contextlib` to read its own output. It needs no `arcpy`, no network, no credentials and
no geodatabase, and it gives the same 181 assertions on Windows and on Linux.

A real run needs `arcpy`, so run it with the Python that ships with ArcGIS Pro.

```
git clone https://github.com/uhsear/xydrift.git
python xydrift.py --self-test
```

## Usage

The check is read-only. Nothing is written without `--apply`.

```
python xydrift.py --layer demo.gdb/Sensors
python xydrift.py --layer demo.gdb/Sensors --tolerance 0.5 --tolerance-units meters
python xydrift.py --layer demo.gdb/Sensors --x-field PT_X --y-field PT_Y
python xydrift.py --layer prod.sde/Sensors --workspace prod.sde --apply
```

A real run against a seven row layer. Two points were moved about 11 m without their columns, one
row was never populated, and one row has no geometry.

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

Every stored coordinate agrees with its geometry.
```

| Flag | Default | What it does |
|---|---|---|
| `--layer` | none | Point feature class to check. Required. |
| `--x-field` | `X` | Stored longitude or easting column. |
| `--y-field` | `Y` | Stored latitude or northing column. |
| `--tolerance` | `1e-06` degrees | Largest difference that is not drift. Required for a projected layer. |
| `--tolerance-units` | the layer's own | `degrees`, `meters` or `feet`. |
| `--wkid` | `4326` | Coordinate system the stored columns are in. |
| `--workspace` | none | Geodatabase to open an edit session on. Needed for a versioned class. |
| `--limit` | `10` | Rows listed before the report counts the rest. |
| `--apply` | off | Write the resynced coordinates. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

Exit codes: 0 no drift or the resync finished, 1 drift found and not written, 2 the resync failed
part way, 64 usage error. A missing `arcpy` prints the Pro interpreter path and exits 1.

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
  largest drift a lon/lat layer can hold, on a row where nothing moved.
- **The column type.** A String coordinate column is refused before any cursor opens, because
  "-82.1" and "-82.10" are the same place and two different strings. A Single column is refused
  when its own step is coarser than the tolerance. Near longitude 82 a Single resolves to
  `7.6e-06` degrees, and near 180 to `1.5e-05`. Every row would then read as drifted for ever.
- **The column type on a projected layer**, where the same check needs a number the tolerance
  cannot supply. A Single's step grows with the coordinate, so the layer's own extent is read
  first. A state plane easting of 600,000 ft resolves to `0.0625` ft, and a northing of
  1,800,000 ft to `0.125` ft, both coarser than any tolerance an editor would set. The extent is
  stored metadata, so reading it costs no extra pass over the rows.
- **A row with no geometry.** Its stored coordinates are left exactly as they are. A
  recalculation that starts from the geometry nulls them, and that row then has no coordinate at
  all.
- **The writing cursor.** The update cursor opens on the OID and the two columns, and never on a
  geometry token. The geometry is read by a separate cursor. A rounded geometry written back
  would move the point this tool exists to trust.

## Why not Calculate Geometry Attributes

Calculate Geometry Attributes is the right tool when you want every row rewritten, and it is
faster than a cursor. It has no comparison in it. It writes all rows, so it stamps editor
tracking on all rows, and it reports nothing: you cannot ask it which rows were wrong.

That report is most of the value here. "Three rows drifted" and "every row recalculated" answer
different questions, and only the first one tells you an editor moved something last Thursday.

An attribute rule that maintains the columns on insert and update is better than either. It
fixes nothing that is already wrong, because it fires on edit. Use this to repair the layer, then
add the rule so it stays repaired.

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
- Branch coverage of the self-test is 97 percent. The uncovered lines are the `arcpy` import
  failure, the real entry point that follows it, the stub geodatabase's own guards, and the
  self-test's failure-reporting arms. None of them can run offline on a green run.

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
