# Enclosure & Mechanical Design — BCG/SCG Wearable

Discussion document covering the structural design of a wearable BCG/SCG device that houses XIAO nRF52840 + BMI330 + (planned) GNSS + cellular modem + battery, while preserving the cardiac signal-to-noise ratio that makes the device work at all.

This is a **design rationale** document, not an instruction manual. It captures the reasoning behind every mechanical choice so future iterations don't re-derive the same tradeoffs from scratch. For the firmware/electronics setup see `BMI330_Setup.md` in this folder.

---

## 1. Constraints

### 1.1 Volume budget

| Dimension | Max | Notes |
|---|---|---|
| Length | 70 mm | Comfortable on sternum without bridging onto ribs |
| Width | 40 mm | Same |
| Height | 20 mm | Including box walls + battery + boards |

Total volume: **56 cm³**. Smaller than a deck of cards.

### 1.2 Battery

400 mAh LiPo, 25 × 20 × 9 mm. Sets a hard 9 mm height floor for one component.

### 1.3 Components to fit

- XIAO nRF52840 Sense (21 × 17.5 × 3.5 mm)
- 7SEMI BMI330 Nano breakout (~15 × 15 × 2.5 mm)
- Contact temperature sensor (4 × 4 × 1 mm — e.g. MAX30205)
- GNSS module (~10 × 10 × 2.5 mm — e.g. u-blox MAX-M10S)
- Cellular module (~14 × 13 × 2 mm — e.g. Quectel BG95-M3)
- Two antennas (chip antennas, ~5 × 3 × 1.5 mm each)
- Battery (above)
- Wires/connectors

### 1.4 The non-negotiable

**SCG signal preservation.** The cardiac vibration is sub-mg in amplitude. Any mechanical design that damps it makes the project not work. Everything else — RF, thermal, cosmetic — comes second to this.

---

## 2. The core mechanical problem

SCG works because the BMI330 chip vibrates with the chest wall. The chest pushes the chip; the chip's MEMS accelerometer registers the motion.

For the chip to vibrate freely with the heart's tiny mechanical impulse:

> **Whatever mass is mechanically coupled to the chip must be small.**

A heart can move 1 g of sensor by ~1 mg. It cannot meaningfully move 25 g of "sensor + battery + radios + plastic shell." The signal is the same; the inertia opposing it is 25× larger; the resulting acceleration at the IMU is 25× smaller.

Every structural decision below is, ultimately, an attempt to keep the BMI330's effective inertia low while still delivering a usable wearable.

---

## 3. Architecture options considered

### 3.1 Single PCB, double-sided (rejected for SCG)

```
   ┌──── TOP: XIAO + SIM + GNSS + antennas ────┐
   │                                            │
   │              FR4 1.6 mm                    │
   │                                            │
   ├─── BOTTOM: BMI330 + temp ──────────────────┤
   └────────────────────────────────────────────┘
                          ↓ skin
```

**Mechanical assessment:** the BMI330 is rigidly bolted to a 70 × 40 mm board carrying ~12 g of components plus 8 g of battery. The board's effective mass at the IMU is ~20 g. Cardiac signal at the chip drops by ~15-20 dB vs. a bare-board test.

Empirically validated 16.8 dB peak SQI on a bare BMI330 breakout (`session_20260605T202030Z`); single-board predicts ~0-2 dB. **This is not viable for SCG.**

Also has secondary issues: cellular TX bursts induce PCB flex right next to the IMU, SIM module heat conducts through copper to the temp sensor, antenna isolation is hard on a small board.

### 3.2 Two PCBs, stacked, connected by flex cable (recommended)

```
   ┌────── MAIN BOARD: XIAO + SIM + GNSS ──────┐
   ├────────────────────────────────────────────┤
                       │ flex cable (10 mm)
                       │
                  air gap (2-3 mm) — empty
                       │
   ┌────── SENSOR BOARD: BMI330 + temp ─────────┐
   └────────────────────────────────────────────┘
                          ↓ skin
```

**Mechanical assessment:** the BMI330 is mounted to a small (25 × 15 mm) PCB carrying only ~1 g of components. The flex cable is intentionally floppy — it transmits DC (the boards stay together) but absorbs vibration. Sensor board's effective mass is ~1 g. Cardiac signal preserved within 1-3 dB of bare-board.

Trade: two pieces to manufacture, 4-conductor flex cable to route, slightly more complex assembly.

**This is the chosen architecture.** Discussed in detail below.

### 3.3 Two PCBs, separate enclosures (best signal, worst form factor)

Sensor PCB taped directly to skin; everything else in a separate clipped-on box. ~95% of bare-board signal preserved. Rejected because it isn't a wearable single object.

---

## 4. Two-board design — what's between the boards

The defining property of the two-board design is what occupies the volume between the two PCBs. The wrong contents destroy isolation; the right contents preserve it.

### 4.1 What goes in the gap

| Item | Verdict |
|---|---|
| **Air** | ✅ ideal |
| Flex cable (4 conductors, polyimide) | ✅ required — only electrical path |
| Battery | ❌ adds 8 g rigidly between boards, kills isolation |
| Foam | ❌ resonates in cardiac band, distorts signal |
| Rigid spacer / pillar | ❌ creates rigid bridge, defeats the gap |
| Glue or solid adhesive | ❌ same as rigid spacer |

**Rule:** the only thing connecting the two boards is the flex cable. The cable is intentionally too soft to transmit cardiac-band vibrations from board to board.

### 4.2 Why nothing rigid between

A rigid pillar between the two boards is mechanically equivalent to making them one board again. Cardiac vibration at the sensor has to push through the pillar, which means it has to move the heavy main board too. Same problem as the single-board design.

This is why "use a spacer to set the gap height" is wrong — the spacer becomes a bridge that defeats the isolation.

### 4.3 So how do we hold the boards in place?

**Each board is mounted to the enclosure separately, not to the other board.**

```
   ┌─────── BOX TOP WALL (rigid PLA, 1.5 mm) ──────┐
   │      ↓ post (2 mm) holds main board           │
   │      ↓                                         │
   │  ┌──────── MAIN BOARD ─────────┐               │
   │                                                 │
   │              air gap                            │
   │                       │                         │
   │                       │ flex cable              │
   │                                                 │
   │  ┌──────── SENSOR BOARD ───────┐               │
   │      ↑                                         │
   │      ↑ pressed against thin patch              │
   ├─── BOX BOTTOM WALL (PLA, see § 5) ─────────────┤
                          ↓ skin
```

Box top has a post going down → holds the main board. Box bottom has a patch going up → holds the sensor board. The two halves of the enclosure are connected at the **perimeter**, not in the middle.

This still has a residual issue: the box itself becomes a long mechanical path between the two boards. A heart pulse at the sensor pushes the box bottom, which pushes the perimeter, which (eventually) pushes the box top, which (eventually) reaches the main board. The path is long enough and the box is compliant enough that this damping is much smaller than a direct bridge — but it isn't zero. § 5 addresses this.

---

## 5. The "thin patch" — local compliance via geometry

### 5.1 The problem we're still solving

Even with two boards and an air gap, if the **whole rigid box** has to vibrate to vibrate the sensor PCB, the heavy electronics in the top half resist. We need the box's bottom-wall patch directly under the sensor to flex *locally* with cardiac motion, while the rest of the box stays stiff for structure.

### 5.2 The principle

Bending stiffness of a flat plastic plate scales with **thickness cubed**. Halving the thickness reduces stiffness by 8×. So a thin patch *of the same material* as the rest of the box can act as a mechanically compliant region — purely by being thinner.

### 5.3 Specific recipe

```
   inside view of box bottom wall:
   
   ┌──────────── 1.5 mm thick PLA (rigid structure) ───────────┐
   │                                                            │
   │            ┌───────── 0.4 mm thick PLA ────────┐           │
   │            │   8 × 8 mm patch directly under   │           │
   │            │   the BMI330 chip                  │           │
   │            │   NO RIBS / supports across patch  │           │
   │            └────────────────────────────────────┘           │
   │                                                            │
   └────────────────────────────────────────────────────────────┘
                          ↓ skin contacts the thin patch
```

- **8 × 8 mm:** big enough to cover the BMI330 footprint. Smaller is fine; bigger reduces stiffness too much.
- **0.4 mm thick:** ~50× more compliant than 1.5 mm. Stiffness still high enough that resonance frequency is in kHz (well above SCG band — see § 7).
- **No ribs across the patch:** ribs reintroduce stiffness. The patch must be unsupported except at its edges.
- **Sensor PCB pressed against this patch from inside:** held by friction + ribs at its perimeter, not at its center.

### 5.4 Why this works

- For high-frequency signals (cardiac valve transients at 5-25 Hz), the patch is **stiff enough** that it doesn't act like a mass-spring system — i.e., it doesn't resonate within the cardiac band.
- For locally-applied force (skin pushing up on a small area under the BMI330), the patch is **compliant enough** to flex without dragging the rest of the rigid box with it.
- The rest of the box (1.5 mm walls, holding the heavy main board) stays effectively rigid and unmoving.
- Result: the BMI330 sees ~80% of bare-board signal, while the device is still a single rigid-feeling enclosure.

Predicted SQI: 8-12 dB (a few dB below bare-board's 16 dB but well above any single-board design).

---

## 6. Why "soft material" alone is wrong

A natural extension of "make the patch flexible" is "use rubber/silicone/foam instead of thin plastic." This is the right instinct in some cases but wrong here.

### 6.1 The mass-spring model

Sensor PCB sitting on a flexible material is a classic mass-spring-damper:

```
   skin
   ────────────────────
        ↓ Force F at frequency f
   ▓▓▓▓▓ flexible material (stiffness k) ▓▓▓▓▓
        ↓
   sensor PCB (mass m ≈ 1 g)
```

Natural resonance: `f_res = (1/2π) × √(k/m)`. Above f_res, the material **damps the signal** instead of transmitting it.

| Material | Stiffness | f_res with 1 g | Verdict for SCG (5-25 Hz) |
|---|---|---|---|
| Soft foam | 1-10 N/m | 5-15 Hz | ❌ resonates IN cardiac band — distorts signal |
| Memory foam | 50-100 N/m | 35-50 Hz | ⚠ borderline; distorts upper cardiac band |
| Soft silicone (Shore 20A, 1 mm) | 300-500 N/m | 85-110 Hz | ✅ ideal |
| Medium silicone (Shore 40A) | 1000+ N/m | 150+ Hz | ✅ excellent |
| 3D-printable TPU (Shore 95A) | 5000 N/m | ~350 Hz | ✅ excellent |
| 0.4 mm PLA patch | ~50,000 N/m | >1000 Hz | ✅ essentially rigid in cardiac band |
| 1.5 mm PLA wall | very high | ~kHz | ⚠ rigid — couples to main-board mass too well |

The "thin PLA patch" sits in the right place: stiff enough that f_res is far above 25 Hz (no distortion), but globally compliant enough that it flexes locally without involving the rest of the rigid box.

### 6.2 The corollary

**Do not use foam under the sensor.** Tempting, but its low stiffness puts the resonance inside the cardiac band, distorting the signal you're trying to measure.

**Do not use a thick rubber pad either.** Same family of failure.

**Soft silicone** (Shore 20A or harder) is acceptable; it's used in commercial wearable patches. But it's harder to manufacture in a 3D-printed prototype.

For a 3D-printed prototype, the **0.4 mm thin-PLA patch** is the simplest, cheapest, and most robust solution.

---

## 7. Why the BMI330 chip orientation doesn't matter (much)

The BMI330 is a 6-axis accelerometer. Whether the chip-side faces skin or away makes no difference to the signal — the MEMS sensor inside the chip is rigidly bonded to the PCB pad, and PCB FR4 is plenty stiff to transmit vibration in either direction.

What does matter:
- **The bare PCB underside** is a smooth flat surface. Better skin contact than a chip's plastic IC package.
- **Components on the top side** (chip + headers) need clearance — they shouldn't be smashed against the box bottom wall.

So the recommendation is: chip faces UP (away from skin), bare PCB underside touches the inside of the thin patch (or the skin directly, in the bare-board configuration).

---

## 8. Battery placement

The 25 × 20 × 9 mm battery has to live somewhere. Options:

### 8.1 Battery beside the main board (chosen)

```
            70 mm
   ┌──────────────────────────────────────────┐
   │                                           │
   │ ┌──── MAIN PCB ────┐  ┌──── battery ─────┐│
   │ │  ~40 × 30 mm     │  │ 25 × 20 × 9 mm   ││
   │ └───────────────────┘ └───────────────────┘│
   │                                           │
   │  ───── air gap with flex cable ──────     │
   │                                           │
   │ ┌──── SENSOR PCB ────┐                    │
   │ └────────────────────┘                    │
   └──────────────────────────────────────────┘
                ↓ skin (only sensor patch)
```

Battery sits at the same vertical level as the main board, beside it. It's part of the heavy zone (top of stack), not in the air gap.

**Why:** zero impact on the air gap or sensor isolation. Total stack height stays around 17.5 mm, within the 20 mm budget.

### 8.2 Battery on top of the main board (acceptable alternative)

Stack: box-top / battery / spacer / main board / air gap / sensor board / box-bottom-patch / skin.

Total height ≈ 22 mm — slightly over budget. Doesn't fit unless the box height is extended or the battery is thinner.

### 8.3 Battery between the boards (rejected)

Same failure mode as the spacer: the battery becomes a rigid mass bridging the air gap, destroying isolation. ~8 g of mass coupled directly to the sensor PCB → cardiac signal damped.

**Never do this.**

---

## 9. Connecting the two boards — the flex cable

### 9.1 What it carries

Just 4 conductors:
- **SDA** (I²C data)
- **SCL** (I²C clock)
- **3V3** (power to BMI330 + temp sensor)
- **GND** (return)

That's the entire electrical interface between the boards.

### 9.2 Connection options

| Option | Pros | Cons | Recommended for |
|---|---|---|---|
| **Hand-soldered silicone wires** (28-30 AWG) | Cheapest, no connectors, easy on any PCB | Twist over time, needs strain relief | Prototype #1 |
| **Flat flex (FFC) + ZIF connectors** (0.5 mm pitch, 4-pin) | Industry standard, low profile, swappable, controlled stiffness | Need SMT connector footprints, small-pitch reflow | Production / v2 |
| **Mezzanine board-to-board headers** (rigid pin/socket) | Rigid, no cable to flop | **Defeats mechanical isolation** — rigidly couples boards | Never (for SCG) |

### 9.3 Cable length

Target **10-15 mm**. Just enough to bend cleanly without stress.

- Shorter is mechanically stiffer — counterproductive.
- Longer increases cable capacitance (irrelevant for I²C below 50 mm) and adds resonance modes (cable can swing audibly when tapped).
- 10-15 mm has a clean half-loop bend, low capacitance, no audible resonance.

### 9.4 Cable strain relief

The flex cable's whole job is to be the only mechanical link between the boards — but only the *bending* part of the cable. Anything that pulls or twists the cable transmits force to both boards, defeating the isolation.

Mitigations:
- Anchor the cable to the main board with a small adhesive clip.
- Tape the cable flat against the inside of the box wall along its run, so it can flex but not swing.
- Avoid sharp bends near the connector (5 mm minimum bend radius).
- Don't route the cable through any tight gaps that would compress it.

---

## 10. Skin contact zone — what touches the user

The wearable touches skin only at the **thin patch on the box bottom wall** (8 × 8 mm). Everywhere else, the box has a slight standoff (~3-5 mm) so its weight is distributed onto the chest via the perimeter, not the sensor zone.

### 10.1 Box bottom wall design

```
            outside (skin side):
   ──────────────────────────────────────────
   │  smooth, slightly rounded edges         │
   │                                          │
   │    ┌──── 8 × 8 mm thin patch ────┐      │
   │    │ slightly recessed by ~0.5mm │      │
   │    └─────────────────────────────┘      │
   │                                          │
   ──────────────────────────────────────────
            inside (sensor side):
   1.5 mm rib structure, except over patch
```

- **Rounded perimeter edges** — the box can sit on skin for hours without digging in.
- **Slight recess** at the thin patch (~0.5 mm) — guarantees skin contact at the patch even if the surrounding box is slightly raised, e.g., by tape lifting it.
- **Smooth print orientation** — print the bottom wall flat on the build plate, no support structures. A rough surface compresses skin unevenly and changes coupling between sessions.

### 10.2 Temperature sensor window

The contact temperature sensor (MAX30205 or similar) needs a low-thermal-resistance path to skin. Three options:

1. **Thin PLA over sensor (~0.5 mm).** Cheap, prints in one piece. Multi-minute equilibration time, ~1°C low offset. Calibrate offset out in firmware.
2. **Aluminum foil patch through a 5 mm hole in the box.** Sensor presses against foil from inside; skin touches foil from outside. <1 minute equilibration, ~0.2°C offset. Best accuracy.
3. **MLX90614 IR sensor.** Rejected — IR doesn't pass through PLA. Would need a germanium window.

Recommendation: option 1 for prototype, option 2 for production. Place the temp sensor **at least 10 mm** from the BMI330 to avoid thermal cross-contamination from the IMU's own dissipation.

### 10.3 Mounting on the body

Three viable methods, ranked by signal quality:

| Method | SCG signal | Practicality |
|---|---|---|
| **Adhesive medical tape (across the box)** | ✅ best — eliminates finger tremor | Single-use tape per session |
| **Elastic chest strap** | ✅ good — even pressure | Reusable, requires strap slots in box |
| **Finger-press (current method)** | ⚠ adds 1-3 Hz tremor in cardiac band | Easiest but noisy |

For best SQI: tape. For repeatable wear: strap with slots molded into the box's two long edges.

---

## 11. Things that hurt SCG that aren't always obvious

Quick reference of mechanical "smells" that kill cardiac signal, even if everything else looks right:

- **Cable tugging** on the sensor board → injects motion artifact.
- **Foam padding** anywhere between sensor and skin → resonates in cardiac band.
- **Battery glued to sensor PCB** → adds 8 g of inertia.
- **Tight standoffs that couple sensor to main board** → defeats isolation.
- **Box that resonates** (long thin shapes, hollow walls) → injects spurious motion at its modes.
- **Loose PCB** rattling inside the box → contact noise.
- **Air gap filled with foam to "stabilize"** → defeats the whole point of two boards.
- **Wide unsupported wall** between sensor and skin → can drum (resonate at low frequency).
- **Sharp box edges** digging into skin near sensor → distorts contact.

---

## 12. Recommended assembly order

For first-build:

1. Assemble & validate **electronics first** without enclosure — bare BMI330 breakout taped to skin, XIAO and battery beside it. Get a clean 16 dB SQI session as your baseline.
2. Print the **enclosure shell** (rigid 1.5 mm PLA, 0.4 mm thin patch under sensor, post for main-board mount, ribs for sensor-board cradle, perimeter wall, lid).
3. Mount **main board** in the top half via posts (M2 screws into integral plastic posts).
4. Place **sensor board** in the bottom half — slot into the rib cradle. Should sit gently on the thin patch from inside.
5. Connect the boards with the **flex cable**, dressing it through the air gap.
6. Close the lid, screw it down, run a session in the same posture as step 1.
7. Compare SQI step 1 vs step 6. Should be within 2-4 dB. If it dropped 6+ dB, the box has a coupling issue — most likely a rib crossing the thin patch, a tight standoff coupling sensor to main board, or the lid pressing the sensor from the wrong direction.

---

## 13. Open mechanical questions

- **Skin coupling agent** — bare PCB on dry skin works at SQI 16 dB. With a hydrogel pad (like ECG electrodes use), would coupling be tighter? Worth testing.
- **Long-session corrosion** — bare PCB sweat exposure over hours. May need clear nail polish / conformal coating on the sensor board's underside (not over the chip).
- **Strap-mount geometry** — slot positions on the long edges, friction vs. captured-pin closure, and how the strap tension affects sensor coupling.
- **Dog harness adaptation** — different chest curvature, different mount mechanics, fur layer between sensor and skin (defeats SCG; requires shaved patch or BCG-style harness mass coupling).
- **Drop test** — what happens if the device is dropped from chest height. Battery + flex cable likely the failure points.

---

## 14. Summary — the design in one paragraph

The wearable is a single 70 × 40 × 20 mm 3D-printed PLA box with two PCBs inside: a small (~25 × 15 mm) sensor board carrying the BMI330 IMU and contact temperature sensor, sitting at the bottom of the box pressed against an 8 × 8 mm thin (0.4 mm) PLA patch on the box's outer wall; and a larger main board carrying the XIAO nRF52840, GNSS module, cellular modem, and a 400 mAh LiPo battery beside it, mounted at the top of the box. The two boards are connected only by a 10 mm 4-conductor flex cable through an empty 2-3 mm air gap; nothing else bridges that gap. The thin patch is locally compliant (geometry, not material — same PLA as the rest), allowing the sensor PCB to flex with cardiac motion while the rest of the rigid box holds the heavy electronics steady. Skin contact is via the thin patch alone, taped to the lower sternum. This combination keeps the effective inertia at the IMU near 1 g (instead of the 25 g of a fully-coupled box), preserving the cardiac signal at within ~4 dB of bare-board test conditions, while still presenting the user with a single rigid-feeling object.
