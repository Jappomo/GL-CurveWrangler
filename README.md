# GL - Curve Wrangler (WIP PAGE)

**A grab and comb brush for NURBS curves in Maya. No plugin to compile, no hair system to adopt.**

Maya has no brush-based sculpting for curves — the Sculpting shelf is polygon-only, and soft-selected CVs are as close as you get out of the box. Curve Wrangler adds XGen-style groom brushes that work directly on plain NURBS curves, in place, non-destructively enough to undo.

![demo](docs/demo.gif)

---

## Why

If you groom with curves — hair cards, fur guides, cables, wires, feathers, foliage — the existing options are all compromises. XGen means converting to guides and back. Ornatrix means committing to its operator stack. The free brush tools that did this are 10+ years old and no longer load. Curve Wrangler is a single Python file that brushes the curves you already have.

## Features

- **Grab** — drag CVs freely with a smooth screen-space falloff.
- **Comb** — motion ramped root-to-tip with the root pinned, then segment lengths re-solved so strands sweep instead of stretching.
- **Auto-Mask** — keep a whole groom selected and still brush one strand at a time. The brush affects only the curve nearest the cursor.
- **Live brush overlay** — a cursor ring showing radius and falloff, plus a readout naming exactly which curve you're about to affect.
- **Proper undo** — one chunk per stroke.
- **Activation hotkey** — register any combination from inside the tool.
- **Themeable accents** — sampled from Maya's own palette, swappable from the gear.

## Install

**Drag and drop.** Download the latest release, unzip, and drag `install.py` into a Maya viewport. It copies one file into your user scripts folder and adds a shelf button. Nothing outside your Maya preferences is touched.

**Manual.** Copy `GL_CurveWrangler.py` into your scripts folder:

| OS | Path |
|---|---|
| Windows | `C:\Users\<you>\Documents\maya\<version>\scripts` |
| macOS | `~/Library/Preferences/Autodesk/maya/<version>/scripts` |
| Linux | `~/maya/<version>/scripts` |

Then run:

```python
import GL_CurveWrangler
GL_CurveWrangler.show()
```

## Usage

Select one or more NURBS curves, hit **ACTIVATE**, and brush.

| Control | Action |
|---|---|
| `LMB` drag | Brush the curves |
| `MMB` drag left/right | Resize the brush radius |
| `Ctrl` + `LMB` | Flip Grab / Comb for one stroke |

### Settings

| Setting | What it does |
|---|---|
| **Radius** | Brush size in screen pixels. |
| **Strength** | How much of your drag reaches the curve. Lower values build shape up in passes. |
| **Root lock** | CVs at the start of each curve that never move. Set to 2 to also hold the root tangent. |
| **Tip bias** | Comb only. 1.0 is a linear root-to-tip ramp; higher stiffens the base and sweeps only the ends. |
| **Auto-Mask** | Restrict each stroke to the curve nearest the cursor. |
| **Preserve Length** | Re-solve segment lengths after each stroke so strands don't stretch. |

### Tips

- If a curve won't hold the shape you want, `Curves → Rebuild` it first with more CVs. Too few CVs and it can't bend; too many and it goes wavy.
- Turn Preserve Length **off** when you deliberately want combing to lengthen curves.
- Auto-Mask plus a large radius is the fastest way to work through a dense groom.

## Compatibility

| | |
|---|---|
| Maya | 2022 – 2026 (PySide2 and PySide6 both supported) |
| OS | Windows, Linux, macOS |
| Dependencies | None. Ships as one file, no compiled plugin. |

Closed (periodic) curves are supported but skip the length solver.

## Known limitations

- On high-DPI displays the brush ring may sit offset from the cursor. Set `DPI_SCALE` at the top of the module to your display scaling.
- Performance degrades in the low thousands of curves; the hover probe subsamples, but the press pass projects every CV.
- The first hotkey assignment creates a hotkey set named `GL_Tools`, because Maya's default set is read-only.

## Planned Features for next version
- Additional brush modes (smooth, noise, scale, clump)
- A better and more perfomant viewport2.0 Override for the RingBrush
- Curve Visibility Helper
- Open to ideas

## License

MIT. Do what you like with it, including commercially. See [LICENSE](LICENSE).

## Credits

Built by Giacomo Liberio with Claude Support. 

If it saves you time on a groom, a screenshot of what you made with it is the best thanks.
