from __future__ import annotations

from desk_monitor import sysstat


def test_cpu_meter(tmp_path):
    stat = tmp_path / 'stat'
    stat.write_text('cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3\n')
    meter = sysstat.CpuMeter(stat)
    stat.write_text('cpu  150 0 150 900 0 0 0 0 0 0\n')
    assert meter.percent() == 50.0
    assert meter.percent() is None  # 変化がなければ None
    assert sysstat.CpuMeter(tmp_path / 'missing').percent() is None


def test_memory_and_uptime(tmp_path, monkeypatch):
    meminfo = tmp_path / 'meminfo'
    meminfo.write_text('MemTotal:       1000 kB\nMemFree:  100 kB\nMemAvailable:    250 kB\n')
    monkeypatch.setattr(sysstat, '_MEMINFO', meminfo)
    assert sysstat.read_memory() == 75.0
    uptime = tmp_path / 'uptime'
    uptime.write_text('12345.67 999.0\n')
    monkeypatch.setattr(sysstat, '_UPTIME', uptime)
    assert sysstat.read_uptime() == 12345.67
    monkeypatch.setattr(sysstat, '_MEMINFO', tmp_path / 'missing')
    assert sysstat.read_memory() is None


def test_snapshot_without_raspberry_pi(monkeypatch, tmp_path):
    monkeypatch.setattr(sysstat, '_THERMAL', tmp_path / 'missing')
    monkeypatch.setattr(sysstat, '_HWMON', tmp_path / 'missing')
    snap = sysstat.snapshot(sysstat.CpuMeter(tmp_path / 'missing'))
    assert snap['temp'] is None and snap['cpu'] is None and snap['throttled'] is None
    assert set(snap) == {'host', 'temp', 'cpu', 'throttled', 'memory', 'uptime'}


def test_read_temp_prefers_cpu_sensor(tmp_path, monkeypatch):
    # PC では thermal_zone0 が基板(acpitz)のことがあるので、CPU のセンサーを優先する
    for name, value, sensor in (('hwmon0', 20000, 'acpitz'), ('hwmon1', 41850, 'nvme'),
                                ('hwmon2', 82250, 'k10temp')):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'name').write_text(sensor + '\n')
        (tmp_path / name / 'temp1_input').write_text(f'{value}\n')
    zone = tmp_path / 'zone'
    zone.write_text('20000\n')
    monkeypatch.setattr(sysstat, '_HWMON', tmp_path)
    monkeypatch.setattr(sysstat, '_THERMAL', zone)
    assert sysstat.read_temp() == 82.25
    (tmp_path / 'hwmon2' / 'name').write_text('other\n')
    assert sysstat.read_temp() == 20.0  # CPU のセンサーがなければ thermal_zone0
