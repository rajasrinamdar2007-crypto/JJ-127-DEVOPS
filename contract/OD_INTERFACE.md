# Detection -> Platform Interface (v1)

The single agreement between the upstream detection component and the rest of the platform.
Every service downstream of `ingest` is written against `contract/event_contract.json`; if this
document and that file ever disagree, **the JSON file wins**.

## Scope

The detection component is a **black box** for this repo. It answers exactly one question —
*which plate, how sure, where, when* — by publishing one JSON message per sighting.

Plate OCR, detection and tracking are **out of scope** and are not built here. Until a real
detector is integrated, the messages are synthetic JSON produced by a local publisher against this
contract, and the rest of the pipeline is exercised end to end against those fakes.

## Delivery

- **One MQTT PUBLISH per sighting.** No batching, no arrays, no envelope.
- Topic: `anpr/{camera_id}/sightings`
- QoS: `1`
- Payload: the event object, verbatim, as UTF-8 JSON.
- The `camera_id` in the payload **must** equal the `{camera_id}` segment of the topic. A mismatch
  is dropped: it means the publisher and the camera identity disagree, and guessing which one is
  right would silently misplace the sighting.

```
topic:   anpr/cam-pune-hinjewadi-01/sightings
payload: {"plate_string": "MH12AB1234", "confidence": 0.94, "camera_id": "cam-pune-hinjewadi-01",
          "lat": 18.5913, "lon": 73.7389, "timestamp": "2026-09-26T07:42:11Z",
          "image_ref": "s3://anpr/cam-pune-hinjewadi-01/2026/09/26/074211.jpg",
          "direction": "E", "lane": 2, "vehicle_type": "car"}
```

## Format

| Field          | Required | Type            | Meaning |
|----------------|----------|-----------------|---------|
| `plate_string` | yes      | string          | Registration as read, uppercased, no spaces or separators |
| `confidence`   | yes      | number          | Detector's own score, `0.0`–`1.0` inclusive |
| `camera_id`    | yes      | string          | Camera identity, must match the topic |
| `lat`          | yes      | number          | WGS84 decimal degrees, `-90`–`90` |
| `lon`          | yes      | number          | WGS84 decimal degrees, `-180`–`180` |
| `timestamp`    | yes      | string (ISO8601)| UTC, moment of the sighting |
| `image_ref`    | no       | string \| null  | Opaque reference to the captured frame |
| `direction`    | no       | string \| null  | Travel direction, from the enum below |
| `lane`         | no       | int \| null     | Lane number within the camera's field of view |
| `vehicle_type` | no       | string \| null  | Vehicle class, from the enum below. `null` means the detector could not classify it |

## Vehicle type enum

`car` | `bike` | `truck` | `bus` | `null`

Classification is **yours alone**. The platform stores whatever you send and never infers a class
from anything else — not from the plate, not from the lane, not from the time of day. A `null` is a
legitimate answer and is stored as such; a guessed class is worse than an honest gap, because
downstream the class selects a map pin and a filter, and a wrong pin is believed.

## Direction enum

`N | S | E | W | NE | NW | SE | SW`

## Rules

1. **`plate_string` is the plate, not the description.** Uppercase, no spaces, no separators
   (`MH 12 AB 1234` → `MH12AB1234`). Ingest stores it as given; anything more aggressive is the
   detection component's business, not ours.
2. **`confidence` is not rescaled.** `0.94` means `0.94`. Ingest persists whatever arrives;
   thresholding is a query-time concern for the GIS API, so re-tuning the cut-off never requires
   re-publishing history.
3. **Position comes from the camera, not from a moving vehicle.** `lat`/`lon` are the camera's own
   surveyed position. There is no GNSS receiver in this loop and no vehicle to fuse with.
4. **`timestamp` is edge-local and therefore untrusted-but-recorded.** Detection runs at the edge
   on a camera that has no guaranteed clock. Ingest records its own `received_at` alongside it and
   keeps both; it does not second-guess the producer.
5. **Unknown keys are preserved, not rejected.** Forward compatibility: a newer detector may send
   fields this version has never heard of, and they must not cost us the sighting.
6. **Invalid messages are logged and dropped, never retried forever.** A payload that fails the
   contract is a producer bug, and QoS 1 redelivery would just replay the same bug at us. Dropped
   messages are logged with the specific reason and are *not* stored anywhere — if you need to
   replay them later, that requires a dead-letter store this platform does not have yet.

## Out of scope

- **Plate OCR / detection / tracking.** Not built here. Synthetic JSON stands in until a real
  detector exists.
- **Image handling.** `image_ref` is an opaque string. We store and return it; we do not fetch,
  decode, resize or serve frames.
- **Storage, dedup and rendering.** Ingest, PostGIS and the GIS API own those. The detection
  component is not asked for ids, retention, or display hints.
