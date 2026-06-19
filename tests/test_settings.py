"""Tests for IMP-054: SessionConfig, MicPlacement, geometry override, SettingsMenu."""

import pytest
from pathlib import Path
from models.session import SessionConfig, MicPlacement


class TestMicPlacement:

    def test_has_distances_true_when_both_speakers_set(self):
        mp = MicPlacement(speaker_l_to_mic=6.5, speaker_r_to_mic=7.2)
        assert mp.has_distances is True

    def test_has_distances_false_when_only_one_set(self):
        mp = MicPlacement(speaker_l_to_mic=6.5)
        assert mp.has_distances is False

    def test_has_distances_false_when_neither_set(self):
        mp = MicPlacement()
        assert mp.has_distances is False

    def test_status_not_measured(self):
        mp = MicPlacement()
        assert 'not measured' in mp.status

    def test_status_set_not_confirmed(self):
        mp = MicPlacement(speaker_l_to_mic=6.5, speaker_r_to_mic=7.2)
        assert 'not confirmed' in mp.status

    def test_status_confirmed(self):
        mp = MicPlacement(speaker_l_to_mic=6.5, speaker_r_to_mic=7.2,
                          distances_confirmed=True, measured_at='14:32')
        assert 'confirmed' in mp.status
        assert '14:32' in mp.status

    def test_as_geometry_dict_correct_keys(self):
        mp = MicPlacement(speaker_l_to_mic=6.5, speaker_r_to_mic=7.2,
                          sub_l_to_mic=6.8, sub_r_to_mic=7.0)
        d = mp.as_geometry_dict()
        assert d['mic_to_top_left_m']  == 6.5
        assert d['mic_to_top_right_m'] == 7.2
        assert d['mic_to_sub_left_m']  == 6.8
        assert d['mic_to_sub_right_m'] == 7.0

    def test_as_geometry_dict_excludes_none(self):
        mp = MicPlacement(speaker_l_to_mic=6.5, speaker_r_to_mic=7.2)
        d = mp.as_geometry_dict()
        assert 'mic_to_sub_left_m'  not in d
        assert 'mic_to_sub_right_m' not in d

    def test_as_geometry_dict_empty_when_no_distances(self):
        mp = MicPlacement()
        assert mp.as_geometry_dict() == {}

    def test_mic_height_default(self):
        mp = MicPlacement()
        assert mp.mic_height == 1.5


class TestSessionConfig:

    def test_save_and_load_roundtrip(self, tmp_path):
        s = SessionConfig(
            date='2026-06-13',
            venue_id='marina_outdoor',
            x32_ip='192.168.1.45',
            x32_port=10023,
            notes='June 13 outdoor show',
        )
        s.mic_placement.speaker_l_to_mic = 6.5
        s.mic_placement.speaker_r_to_mic = 7.2
        s.mic_placement.distances_confirmed = True
        s.mic_placement.measured_at = '17:30'
        fpath = tmp_path / 'session.yaml'
        s.save(fpath)
        s2 = SessionConfig.load(fpath)
        assert s2.date    == '2026-06-13'
        assert s2.venue_id == 'marina_outdoor'
        assert s2.x32_ip  == '192.168.1.45'
        assert s2.notes   == 'June 13 outdoor show'
        assert s2.mic_placement.speaker_l_to_mic == 6.5
        assert s2.mic_placement.speaker_r_to_mic == 7.2
        assert s2.mic_placement.distances_confirmed is True
        assert s2.mic_placement.measured_at == '17:30'
        assert s2.mic_placement.mic_height == 1.5

    def test_load_missing_file_returns_default(self, tmp_path):
        s = SessionConfig.load(tmp_path / 'nonexistent.yaml')
        assert s.venue_id == ''
        assert s.x32_ip  == ''
        assert s.mic_placement.distances_confirmed is False
        assert s.mic_placement.has_distances is False

    def test_load_corrupt_yaml_returns_default(self, tmp_path):
        fpath = tmp_path / 'bad.yaml'
        fpath.write_text("{{invalid yaml{{")
        s = SessionConfig.load(fpath)
        assert s.venue_id == ''

    def test_save_creates_directory(self, tmp_path):
        s = SessionConfig(date='2026-06-13', venue_id='test_venue')
        fpath = tmp_path / 'new_dir' / 'session.yaml'
        s.save(fpath)
        assert fpath.exists()

    def test_archive_creates_dated_file(self, tmp_path, monkeypatch):
        import models.session as ms
        monkeypatch.setattr(ms, 'SESSIONS_DIR', tmp_path)
        s = SessionConfig(date='2026-06-13', venue_id='marina')
        s.save(tmp_path / 'latest_session.yaml')
        s.archive()
        files = list(tmp_path.glob('2026-06-13_marina.yaml'))
        assert len(files) == 1

    def test_archive_no_op_when_no_date(self, tmp_path, monkeypatch):
        import models.session as ms
        monkeypatch.setattr(ms, 'SESSIONS_DIR', tmp_path)
        s = SessionConfig(venue_id='marina')   # no date
        s.archive()
        assert list(tmp_path.glob('*.yaml')) == []

    def test_none_distances_round_trip_as_none(self, tmp_path):
        """Sub distances left as None should round-trip as None, not as 0."""
        s = SessionConfig()
        s.mic_placement.speaker_l_to_mic = 6.5
        s.mic_placement.speaker_r_to_mic = 7.2
        fpath = tmp_path / 'session.yaml'
        s.save(fpath)
        s2 = SessionConfig.load(fpath)
        assert s2.mic_placement.sub_l_to_mic is None
        assert s2.mic_placement.sub_r_to_mic is None


class TestGeometrySessionOverride:

    def test_session_distances_override_venue_yaml(self):
        """Session rangefinder distances should override venue YAML computed distances."""
        from core.geometry import load_venue_profile

        p1 = load_venue_profile('outdoor_patio_june13')
        dist_without = p1.geometry.dist_mic_to_top_left_m

        s = SessionConfig(venue_id='outdoor_patio_june13')
        s.mic_placement.speaker_l_to_mic = 12.0
        s.mic_placement.speaker_r_to_mic = 13.0
        p2 = load_venue_profile('outdoor_patio_june13', session=s)

        assert abs(p2.geometry.dist_mic_to_top_left_m - 12.0) < 0.01
        assert abs(p2.geometry.dist_mic_to_top_left_m - dist_without) > 1.0

    def test_right_speaker_distance_overridden_independently(self):
        from core.geometry import load_venue_profile

        s = SessionConfig(venue_id='outdoor_patio_june13')
        s.mic_placement.speaker_l_to_mic = 6.5
        s.mic_placement.speaker_r_to_mic = 7.2
        p = load_venue_profile('outdoor_patio_june13', session=s)

        assert abs(p.geometry.dist_mic_to_top_right_m - 7.2) < 0.01

    def test_no_session_uses_venue_yaml_positions(self):
        from core.geometry import load_venue_profile
        p = load_venue_profile('outdoor_patio_june13', session=None)
        assert p.geometry.dist_mic_to_top_left_m > 0

    def test_session_without_distances_uses_yaml_positions(self):
        """Session with no rangefinder data should fall back to YAML positions."""
        from core.geometry import load_venue_profile

        p_yaml    = load_venue_profile('outdoor_patio_june13')
        s_empty   = SessionConfig(venue_id='outdoor_patio_june13')
        p_session = load_venue_profile('outdoor_patio_june13', session=s_empty)

        assert abs(p_session.geometry.dist_mic_to_top_left_m -
                   p_yaml.geometry.dist_mic_to_top_left_m) < 0.001

    def test_partial_override_sub_falls_back_to_yaml(self):
        """Speaker L/R overridden but sub not set — sub should fall back to YAML."""
        from core.geometry import load_venue_profile

        p_yaml = load_venue_profile('outdoor_patio_june13')
        s = SessionConfig(venue_id='outdoor_patio_june13')
        s.mic_placement.speaker_l_to_mic = 6.5
        s.mic_placement.speaker_r_to_mic = 7.2
        # sub distances not set — should fall back

        p = load_venue_profile('outdoor_patio_june13', session=s)
        assert abs(p.geometry.dist_mic_to_sub_left_m -
                   p_yaml.geometry.dist_mic_to_sub_left_m) < 0.001


class TestSettingsMenuImports:

    def test_settings_imports_cleanly(self):
        from core.settings import SettingsMenu
        assert SettingsMenu is not None

    def test_settings_instantiates_with_empty_session(self):
        from core.settings import SettingsMenu
        menu = SettingsMenu(session=SessionConfig(), running_show=False)
        assert menu is not None

    def test_settings_instantiates_running_show(self):
        from core.settings import SettingsMenu
        menu = SettingsMenu(session=SessionConfig(), running_show=True)
        assert menu.running_show is True

    def test_venue_name_returns_no_venue_when_empty(self):
        from core.settings import SettingsMenu
        menu = SettingsMenu(session=SessionConfig(), running_show=False)
        assert menu._venue_name() == "no venue"

    def test_session_status_not_set(self):
        from core.settings import SettingsMenu
        menu = SettingsMenu(session=SessionConfig(), running_show=False)
        assert 'not set' in menu._session_status()

    def test_is_outdoor_false_when_no_venue(self):
        from core.settings import SettingsMenu
        menu = SettingsMenu(session=SessionConfig(), running_show=False)
        assert menu._is_outdoor() is False


class TestSettingsMenuThreadSafety:

    def test_settings_menu_instantiates_in_non_main_thread(self):
        """SettingsMenu must be instantiatable from a daemon thread without deadlock."""
        import threading
        from core.settings import SettingsMenu

        errors  = []
        results = {}

        def _run():
            try:
                menu = SettingsMenu(session=SessionConfig(), running_show=True)
                assert menu is not None
                assert menu.running_show is True
                results['ok'] = True
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=2.0)

        assert not errors, f"SettingsMenu raised in thread: {errors}"
        assert results.get('ok'), "SettingsMenu did not complete in thread"
