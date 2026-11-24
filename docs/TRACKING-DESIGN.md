# AI-assisted tracking design and implementation plan

## Scope and current state

The AI chooses targets, prepares trajectories, supervises alignment and explains
status. A deterministic local producer handles frequent updates; AI response
latency must not be part of the motor timing loop. The [engineering log](engineering/2026-11-16-tracking-constraints.md)
preserves the operator's ISS and Oceanside launch scenarios and their limitations.

| Capability | Current implementation / boundary |
| --- | --- |
| Timed motor jogs and encoder readback | Production motor backend; 36 calibration pulses completed on this device |
| MCP supervision | Status, discovery, diagnostics, jobs, motor/bridge control, deliberate settings writes |
| High-rate target ingress | Authenticated localhost HTTP, sequence and UTC validation, bounded latest samples |
| Simulation | Rate-limited local model; stale/invalid input stops simulation; not hardware validation |
| Native position targets | Experimental AUX 0x02, explicit commissioning override and arm; configurable 1–20 Hz loop, conservative 4 Hz default, no timing guarantee |
| Connection recovery | Experimental worker retries with backoff; needs both fresh encoders and a new target after reconnection |
| Local reference | Paused worker can map encoder position to known AZ/EL; does not create shared celestial alignment |
| Divergence detection | Prototype pauses/disarms when target error increases beyond its guard |
| Predictive hardware velocity / arrive-by-time | Not implemented or advertised as supported |
| Orbitron, Flight Club, TLE, rotctld adapters / pass graphics | Planned external producers, not implemented |
| Whole-pass mechanical safety | Not verified; prototype limited to at most 5 degrees from initial encoder position |

Native hardware commissioning could not obtain a fresh encoder pair; it stopped
before sending a target. The 5-degree tracking test remains pending. A working
TCP connection, passing simulator test or successful timed jog is insufficient
to mark native tracking supported.

## Control contract

Call `control_capabilities` before selecting a mode. MCP manages connection,
reference, explicit arming, status and stop. For sustained input use the URL and
token file returned by `target_stream_start`; send POST /target JSON:

```json
{"sequence": 1, "utc": 1794866400.0, "azimuth_deg": 123.4, "elevation_deg": 35.6}
```

The timestamp is Unix UTC seconds and must be replaced with the current target
time. AZ is north-zero/east-increasing in the producer's agreed reference;
elevation is above the local horizon. The controller only knows the local
reference supplied to it. A single reference cannot correct mount tilt or build
a full sky model. No endpoint for shared COSMOS celestial alignment is claimed.

Start in simulation. Stream limits default to elevation 0–80 degrees and 2.5
deg/s on each axis; these are software defaults, not certified hardware limits.
The server rejects malformed/nonfinite samples, old or non-increasing sequence
and timestamps, samples more than 0.2 seconds ahead, samples older than the
configured stale threshold (default 0.5 seconds), and excessive per-axis rates.
Two samples are retained; simulated extrapolation is bounded. No queued replay
of old trajectories is allowed. Hardware updates are serialized and may be much
slower than the incoming sample rate. Report encoder age and connection state
alongside every decision; an accepted target is not proof of movement or arrival.

The experimental native worker requires an explicit travel envelope, fresh
feedback, reference and arming. After a link loss it attempts Stop and reconnects
with 0.5–2 second backoff. Fresh feedback plus a new post-reconnection target is
required before resuming. The last position remains visible with age. Suspected
power cycles invalidate reference assumptions and require realignment. A finite
last target can still complete if Stop cannot be delivered; software limits do
not replace mechanical stops or operator clearance.

`position_control_start` accepts `dispatch_rate_hz` from 1 through 20. The
conservative default remains 4 Hz (250 ms cycle). Any value above 4 Hz uses an
interval below 250 ms and returns an explicit experimental/unverified warning.
Rates above 10 Hz are stress-test requests. This is not a claim that the mount
sustains 10, 15 or 20 Hz:
each target cycle still serializes two encoder reads and, when a target changes,
two motor-target request/reply exchanges. If those exchanges consume the cycle
budget, the actual rate is lower. Commission faster settings incrementally while
recording reply timeouts, encoder age, loop jitter and Stop behavior.
Status exposes the configured interval, completed-cycle count, overrun count,
last cycle duration and maximum cycle duration. An overrun means the serialized
AUX work already exceeded the requested interval; it is evidence that the chosen
rate was not achieved, even if no connection timeout occurred.

## Producer plan

The Pi dashboard provides a guarded commissioning baseline with thresholded or
CSRT/KCF/MOSSE Feature ROI tracking, bounded encoder-target output, short optional
predictive coast, velocity feed-forward/proportional lead, blink/dropout testing,
and separate image/encoder error plots. It is not validated autonomous tracking.
A full external PID is intentionally avoided because the mount already closes its
internal position loop. Automatic backlash estimation/application remains future
work and must preserve explicit readback, approval and rollback.

1. **Live AZ/EL:** normalize current UTC, sequence, frame conventions and source
   age. Keep only the latest useful sample and short bounded lookahead.
2. **Orbitron/WispDDE:** validate actual local DDE service/topic/item and payload;
   timestamp on acquisition and preserve source age when provided. Translate to
   the stream. Never replay a buffered DDE backlog after reconnect.
3. **Orbital satellites:** use a tested SGP4 implementation with TLE/OMM epoch,
   UTC and observer latitude/longitude/height. Archive element set and site for
   reproducibility; evaluate azimuth unwrap, acceleration and near-zenith rates.
4. **Powered launch:** consume actual CSV/ephemeris schema; use T+ plus separately
   editable UTC liftoff T0, stage identity and observer coordinates. A hold stops
   timeline advancement. T0 or stage changes require continuity checks before
   motion resumes. Do not substitute a parking-orbit TLE for powered ascent.
5. **Plots:** generate sky track, axis rates/accelerations, visibility windows,
   forbidden travel, encoder age and pointing-error plots outside the GUI/MCP
   motor backend. Highlight infeasible sections before a pass.

## Feasibility, catch-up and drift

Use both axis derivatives, not only total angular speed. Check acceleration,
transport latency, reversal backlash, braking distance, cable wrap and zenith
singularity. Reserve rate margin for catching up. For one axis moving in the
same direction, a first-order estimate is error / (available rate - target rate),
only when the denominator is positive; it omits acceleration and other limits.
Reject deadlines that cannot be met rather than increasing rate beyond hardware
limits. Do not hard-code the provisional launch table as an operating envelope.

Future variable-rate control needs verified AUX command semantics and measured
tracking accuracy before a predictive feed-forward mode can be enabled. A future
`arrive_by_utc` contract must return accepted/rejected feasibility and achieved
error; the current UTC field is freshness, not an arrival deadline. On divergence,
pause/stop, notify the supervisor, correct the reference or trajectory, and
explicitly re-arm. Do not silently rewrite hardware encoder references to chase
drift. A dedicated drift-offset API remains planned.

## Vision tracking candidates to evaluate

These are evaluation items, not implemented or validated telescope-control
capabilities:

- Evaluate a custom compact YOLO detector, initially YOLO11n exported to NCNN,
  for target acquisition and reacquisition. Stock COCO weights do not provide a
  rocket or satellite class; compare manual region selection, custom training and
  brightness/motion acquisition. Do not classify an airplane detection as proof
  of a rocket track.
- Evaluate ByteTrack as a low-overhead detection-association layer. Record track
  ID stability, short-gap recovery and false associations. ByteTrack has no
  appearance ReID or camera-motion compensation, so explicitly test rapid mount
  slews, sparse star fields, clouds, aircraft crossings and target disappearance.
- Compare a hybrid loop in which YOLO runs only for acquisition/reacquisition and
  a centroid, optical-flow, MOSSE/KCF-style or Kalman-assisted tracker updates
  every intervening frame. The detector/tracker must output acquisition time,
  monotonic frame time, target center, bounding box, confidence, track ID and
  stale/lost state.
- Benchmark CSI, USB UVC and future ASI120 adapters behind one grayscale-frame plus
  monotonic-timestamp contract. Camera-specific APIs must not enter the motor
  controller. Measure dropped frames, exposure latency and timestamp uncertainty,
  not only reported camera FPS.
- On the Raspberry Pi 3B+ 32-bit armv7 deployment, compare local NCNN inference,
  reduced input sizes and detector offload. Record detector FPS, per-frame tracker
  FPS, end-to-end latency, CPU/RAM load, temperature and throttling. A faster
  external detector may assist reacquisition, but network/AI latency must never be
  placed in the motor timing or Stop path.
- Calibrate pixel error to angular error across the field of view and validate
  sign, rotation, distortion, camera-to-telescope alignment and exposure effects.
  An image-space track does not establish celestial alignment.
- Keep vision output upstream of the existing guarded target contract. Current
  experimental hardware dispatch defaults to 4 Hz; faster 1–20 Hz settings are
  explicitly unverified. A
  high-frame-rate camera demonstration is not evidence of high-speed mount
  tracking. Stop on stale frames, lost target, stale encoders, transport loss or
  an exceeded mechanical envelope.

References for evaluation: [Ultralytics tracking mode and ByteTrack
configuration](https://docs.ultralytics.com/modes/track), [Ultralytics NCNN
deployment](https://docs.ultralytics.com/integrations/ncnn), and the
[ByteTrack ECCV 2022 paper](https://www.ecva.net/papers/eccv_2022/papers_ECCV/papers/136820001.pdf).
The [Pi vision-stack manifest](PI-VISION-STACK.md) records the installed base,
exact candidate checkouts, compatibility findings and evaluation order.

### Pi vision/control proposal review

The selected split is appropriate: a Raspberry Pi performs capture, prediction,
vision and supervision; a small adapter handles deterministic AUX electrical
timing and a short lost-command watchdog. Satellite prediction plus optical
correction and launch waypoints plus optical correction can share the same guarded
target contract. Keep capture, detector/tracker, control and hardware I/O in
separate bounded queues that overwrite stale work with the newest sample.

Do not carry the following proposal claims into implementation without tests:

- A sub-pixel centroid is not sub-arcminute mount pointing. Plate scale, finder
  alignment, flexure, distortion, exposure smear, backlash and encoder/control
  response all contribute to the final angular error.
- Skyfield can calculate a smooth predicted track, but TLE/source epoch, observer
  coordinates, clock error and propagation/model error limit accuracy. “Current
  microsecond” does not mean microsecond-accurate target truth.
- Background subtraction only cancels stars while the image is registered. Mount
  motion moves the star field, so compensate camera motion or use the predicted
  transform before classifying residuals.
- One or two seconds of blind Kalman continuation is unsafe for a fast target and
  wide mount motion. Make the coast interval short and configurable; stop or enter
  bounded reacquisition when uncertainty/age exceeds the limit.
- The observed COSMOS commands `0x24` and `0x25` select positive and negative
  fixed-rate movement for the addressed axis. The one-byte payload has verified
  discrete speed levels in existing jog tests. It is not yet a calibrated arbitrary
  degrees/second guide-rate interface. Both axes use their own destination address;
  `0x24` is not “azimuth” and `0x25` is not “elevation.”
- A 20 Hz Pi control cadence does not establish a 20 Hz AUX command rate. The
  current guarded position worker defaults to 4 Hz and offers unverified tests up
  to 20 Hz as a stress request. A future rate-command backend needs measured command/reply timing,
  rate-to-degrees/second calibration, reversal/braking tests and watchdog tests.
- The proposed `R,+0.452,-0.128` serial frame would be our own Pi-to-ESP32 protocol,
  not a Celestron command. Include sequence, monotonic age/deadline, checksum or
  CRC, explicit units/mode and a hardware-side timeout that sends zero-rate/Stop.
- The current deployment is a Pi 3B+ with 32-bit armv7 OS. Confirm that the chosen
  ZWO SDK and Python binding support that exact architecture; an ARM64-only package
  will not run. Keep V4L2/CSI/SDK adapters behind the common timestamped-frame API.

On this Pi, the installed 640x480 USB-camera bright-centroid probe processed 96
frames in 5.012 seconds (19.15 FPS, mean processing time 52.0 ms). That validates
capture plus a simple threshold/centroid path only. It does not validate YOLO,
background registration, Kalman/PID, dashboard rendering or telescope actuation at
that rate. Run those stages independently and together while recording latency.

## Acceptance work remaining

- Verify native 0x02 acknowledgement and actual encoder motion with a small
  supervised target, then bounded 5-degree sweep; current attempt sent no motion.
- Measure sustained update latency, paired-axis sampling skew, target error,
  acceleration/braking and reversal response across speed/load/direction.
- Commission the configurable loop above 4 Hz one step at a time. Establish the
  highest repeatable rate separately for SkyQLink/Wi-Fi and a future direct AUX
  transport; automatically reducing the rate after missed deadlines is planned.
- Exercise producer loss, corrupt/stale/future targets, disconnect mid-motion,
  controller restart and target changes during recovery; verify no stale replay.
- Validate actual mechanical clearance and cable limits independently of encoder
  values; reject unsafe route choices and zenith crossings.
- Test acquisition and reacquisition below measured limits with bounded error;
  test impossible rates/deadlines are rejected and reported.
- Run the vision evaluation matrix above first on recorded clips, then live video
  without motor output, then a supervised bounded mount test. Preserve exact model,
  runtime, resolution, thresholds, camera settings and timing results.
- Validate Orbitron/CSV/TLE adapters against reference trajectories and correct
  observer/time/stage; keep those adapters outside this project's current GUI scope.
