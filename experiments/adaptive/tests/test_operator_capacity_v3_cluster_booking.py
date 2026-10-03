from datetime import datetime, timezone

import pytest

from kauri_experiment import operator_capacity_v3_cluster as cluster


BOOKING = 'ah8geq2que22d95lkhr9ttm5bg'
ROW = '| ' + ' | '.join([BOOKING, 'proteina02', 'gascarvalho', 'EXCLUSIVE',
                         '8 hours', '2026-10-03 10:30', '2026-10-03 19:00']) + ' |\n'


def test_today_reservation_has_exact_identity_and_full_original_reserve():
    now = datetime(2026, 10, 3, 9, 30, tzinfo=timezone.utc)
    row = cluster.booking_row(ROW, BOOKING, now, reserve_s=11500)
    result = cluster.booking_coverage(ROW, now, reserve_s=11500)
    assert result['booking_rows'] == [row]
    assert result['fixed_reserve_seconds'] == 11500
    assert result['coverage_until_utc'] == '2026-10-03T18:00:00+00:00'


@pytest.mark.parametrize('old,new', [('proteina02', 'proteina01'),
    ('gascarvalho', 'someone'), ('EXCLUSIVE', 'SHARED'),
    ('2026-10-03 19:00', '2026-10-03 18:30'),
    (BOOKING, '9ju0eqk272j7ofgtkvii0rag68')])
def test_booking_identity_or_bounds_drift_rejects(old, new):
    with pytest.raises(cluster.ClusterError):
        cluster.booking_row(ROW.replace(old, new), BOOKING,
            datetime(2026, 10, 3, 9, 30, tzinfo=timezone.utc), reserve_s=11500)


@pytest.mark.parametrize('hour,minute', [(9, 29), (18, 0), (15, 0)])
def test_inactive_or_insufficient_campaign_reserve_rejects(hour, minute):
    with pytest.raises(cluster.ClusterError):
        cluster.booking_row(ROW, BOOKING,
            datetime(2026, 10, 3, hour, minute, tzinfo=timezone.utc), reserve_s=11500)


def test_listing_dates_are_bound_to_current_authorized_table(monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        assert kwargs == {'text': True, 'timeout': 25}
        return ROW
    monkeypatch.setattr(cluster.subprocess, 'check_output', run)
    assert cluster.booking_listing() == ROW
    command = commands[0]
    assert command[command.index('-s') + 1] == '2026-10-03 10:30'
    assert command[command.index('-e') + 1] == '2026-10-03 19:00'
