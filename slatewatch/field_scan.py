"""Immutable content-only field inspections on the shared programme clock."""
from dataclasses import asdict

from .clock import Span
from .fields import FIELD_INSPECTION_VERSION, FieldInspector, inspection_identity
from .hits import bind_hits
from .media import field_decode
from .rules import FieldThresholds


def measure_fields(asset, path, policy):
    rules = FieldThresholds(**policy['field_thresholds'])
    tracks = asset['media']
    inspector = FieldInspector(tracks['picture'], rules)
    for frame in field_decode(path, tracks):
        inspector.push(frame)
    findings, observed, stats, frames = inspector.finish()
    inspection_id = inspection_identity(asset, policy)
    raster = [{'start_us': frame.timestamp_us, 'duration_us': frame.duration_us,
               'width': frame.width, 'height': frame.height} for frame in frames]
    extent = Span(0, max(entry['end_us'] for entry in observed))
    record = {'id': inspection_id, 'master_id': asset['id'], 'policy_id': policy['id'],
              'analysis_version': FIELD_INSPECTION_VERSION,
              'field_thresholds': asdict(rules),
              'extent': extent.as_dict(), 'tracks': tracks,
              'dominant_field_order': stats['dominant_field_order'],
              'field_order_votes': stats['field_order_votes'],
              'combing_frames': stats['combing_frames'],
              'frames': observed, 'raster': raster,
              'comb_points': stats['comb_points'],
              'hits': bind_hits(inspection_id, findings)}
    return record
