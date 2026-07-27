"""A minimal GTFS-Realtime reader (MASTER_PLAN P5-2 #10).

GTFS-RT is protobuf, and the usual answer is `gtfs-realtime-bindings`, which
pulls in `protobuf` -- a large dependency with a C extension, on an appliance
meant to keep working untouched for years.

The subset actually needed is small: trip updates (which stop, what time) and
service alerts (header, description, which routes). Protobuf's wire format is
self-describing enough to walk without a schema -- every field is a varint tag
carrying a field number and a wire type -- so this decodes the bytes generically
and then picks out the field numbers that matter.

Those numbers come from the GTFS-Realtime specification and are written down
rather than left as magic:

    FeedMessage      entity=2
    FeedEntity       id=1, trip_update=3, alert=5
    TripUpdate       trip=1, stop_time_update=2
    TripDescriptor   trip_id=1, route_id=5
    StopTimeUpdate   arrival=2, departure=3, stop_id=4
    StopTimeEvent    delay=1, time=2
    Alert            informed_entity=5, header_text=10, description_text=11
    EntitySelector   route_id=3, stop_id=5
    TranslatedString translation=1
    Translation      text=1

Deliberately forgiving: an unknown field is skipped and a field that will not
decode as the expected type is ignored rather than raising. A feed that gains a
field must not break the reader.
"""
WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LENGTH = 2
WIRE_32BIT = 5


def _read_varint(data, pos):
    result = shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 70:                       # a varint is at most 10 bytes
            raise ValueError('varint too long')
    raise ValueError('truncated varint')


def decode(data):
    """Bytes -> {field_number: [value, ...]}.

    Length-delimited values are returned as raw bytes; the caller decides
    whether they are a nested message or a string, because the wire format
    does not distinguish them.
    """
    fields = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        number, wire = tag >> 3, tag & 0x07

        if wire == WIRE_VARINT:
            value, pos = _read_varint(data, pos)
        elif wire == WIRE_LENGTH:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ValueError('truncated length-delimited field')
            value, pos = data[pos:pos + length], pos + length
        elif wire == WIRE_64BIT:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == WIRE_32BIT:
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise ValueError(f'unknown wire type {wire}')

        fields.setdefault(number, []).append(value)
    return fields


def _first(fields, number):
    values = fields.get(number)
    return values[0] if values else None


def _sub(fields, number):
    raw = _first(fields, number)
    return decode(raw) if isinstance(raw, (bytes, bytearray)) else None


def _text(fields, number):
    """A TranslatedString -> the first translation's text."""
    translated = _sub(fields, number)
    if not translated:
        return ''
    translation = _sub(translated, 1)
    if not translation:
        return ''
    raw = _first(translation, 1)
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode('utf-8', errors='replace')
    return ''


def parse_feed(data):
    """Bytes -> {'trip_updates': [...], 'alerts': [...]}."""
    trip_updates, alerts = [], []
    try:
        feed = decode(data)
    except ValueError as e:
        raise ValueError(f'Not a readable GTFS-RT feed: {e}')

    for raw_entity in feed.get(2, []):
        if not isinstance(raw_entity, (bytes, bytearray)):
            continue
        try:
            entity = decode(raw_entity)
        except ValueError:
            continue

        update = _sub(entity, 3)
        if update is not None:
            parsed = _parse_trip_update(update)
            if parsed:
                trip_updates.append(parsed)

        alert = _sub(entity, 5)
        if alert is not None:
            parsed = _parse_alert(alert)
            if parsed:
                alerts.append(parsed)

    return {'trip_updates': trip_updates, 'alerts': alerts}


def _parse_trip_update(update):
    trip = _sub(update, 1) or {}
    trip_id = _first(trip, 1)
    route_id = _first(trip, 5)

    stops = []
    for raw_stu in update.get(2, []):
        if not isinstance(raw_stu, (bytes, bytearray)):
            continue
        try:
            stu = decode(raw_stu)
        except ValueError:
            continue
        stop_id = _first(stu, 4)
        arrival = _sub(stu, 2) or {}
        departure = _sub(stu, 3) or {}
        stops.append({
            'stop_id': stop_id.decode('utf-8', 'replace')
                       if isinstance(stop_id, (bytes, bytearray)) else None,
            # `time` is an absolute POSIX timestamp; `delay` is seconds against
            # the schedule. Feeds publish one or the other, rarely both.
            'arrival': _first(arrival, 2),
            'arrival_delay': _first(arrival, 1),
            'departure': _first(departure, 2),
            'departure_delay': _first(departure, 1),
        })

    return {
        'trip_id': trip_id.decode('utf-8', 'replace')
                   if isinstance(trip_id, (bytes, bytearray)) else None,
        'route_id': route_id.decode('utf-8', 'replace')
                    if isinstance(route_id, (bytes, bytearray)) else None,
        'stops': stops,
    }


def _parse_alert(alert):
    routes, stops = set(), set()
    for raw_selector in alert.get(5, []):
        if not isinstance(raw_selector, (bytes, bytearray)):
            continue
        try:
            selector = decode(raw_selector)
        except ValueError:
            continue
        route = _first(selector, 3)
        stop = _first(selector, 5)
        if isinstance(route, (bytes, bytearray)):
            routes.add(route.decode('utf-8', 'replace'))
        if isinstance(stop, (bytes, bytearray)):
            stops.add(stop.decode('utf-8', 'replace'))

    header = _text(alert, 10)
    if not header:
        return None
    return {
        'header': header,
        'description': _text(alert, 11),
        'routes': sorted(routes),
        'stops': sorted(stops),
    }


def summarise(feed):
    """For logs and diagnostics."""
    return (f"{len(feed.get('trip_updates', []))} trip update(s), "
            f"{len(feed.get('alerts', []))} alert(s)")
