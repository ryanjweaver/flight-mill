"""SOFTWARE ONLY: deterministic F03R-T1 artifact selection regression."""
import csv
import json
from pathlib import Path

import pytest

from flightmill.constants import RAW_CSV_HEADER
from stop_tail_support import persisted_rows


@pytest.mark.parametrize('first', ['metadata', 'irrelevant'])
def test_persisted_rows_uses_journal_base_not_glob_order(tmp_path, monkeypatch, first):
    base = 'SOFTWARE_trial1_A1_Attempt1'
    metadata = tmp_path / (base + '_metadata.partial.csv')
    metadata.write_text('field,value\nincomplete,true\n')
    irrelevant = tmp_path / 'unrelated.partial.csv'
    irrelevant.write_text('field,value\nnot_raw,anything\n')
    raw = tmp_path / (base + '.partial.csv')
    with raw.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(RAW_CSV_HEADER)
        writer.writerow(['1', '0', '0.000000', '0', '0.000000', '1', '0.6283185307', '', '0'])
    (tmp_path / (base + '_journal.json')).write_text(json.dumps({'base': base}))
    original_glob = Path.glob

    def ordered_glob(directory, pattern, *args, **kwargs):
        if directory == tmp_path and pattern == '*.partial.csv':
            return iter([metadata, irrelevant, raw] if first == 'metadata'
                        else [irrelevant, metadata, raw])
        return original_glob(directory, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, 'glob', ordered_glob)
    rows = persisted_rows(tmp_path)
    assert len(rows) == 1
    assert list(rows[0]) == list(RAW_CSV_HEADER)
    assert rows[0]['event_n'] == '1'
    assert rows[0]['speed_m_s'] == ''


def test_persisted_rows_rejects_wrong_raw_header(tmp_path):
    (tmp_path / 'chosen_journal.json').write_text(json.dumps({'base': 'chosen'}))
    (tmp_path / 'chosen.partial.csv').write_text('field,value\nwrong,header\n')
    with pytest.raises(AssertionError):
        persisted_rows(tmp_path)
