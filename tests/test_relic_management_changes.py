import sqlite3

from core.loadout_optimizer import _satisfies
from core.official_relics import load_official_relics
from core.relic_inventory import RelicInventory


def test_all_official_relics_are_owned_once_and_locked(tmp_path):
    inventory = RelicInventory(str(tmp_path / 'official.sqlite3'))
    inventory.ensure_official_owned()
    inventory.ensure_official_owned()
    officials = [row for row in inventory.list_owned() if row['official_id'] is not None]
    assert len(officials) == len(load_official_relics()) == 118
    assert len({row['official_id'] for row in officials}) == 118
    with sqlite3.connect(inventory.path) as db:
        assert db.execute("SELECT COUNT(*) FROM scans WHERE scan_id LIKE 'official:%'").fetchone()[0] == 118
    import pytest
    with pytest.raises(ValueError, match='不可删除'):
        inventory.delete_owned([officials[0]['id']])
    with pytest.raises(ValueError, match='不可修改'):
        inventory.save_manual(officials[0]['id'], 'normal', officials[0]['color'],
                              officials[0]['affixes'])


def test_same_named_requirement_matches_either_relic_type():
    condition = {'kind': 'affix', 'name': '打倒敌人时，能累积绝招量表'}
    for mode in ('normal', 'deepnight'):
        assert _satisfies({'relic_type': mode,
                           'affixes': ['打倒敌人时，能累积绝招量表']}, condition)
    assert not _satisfies({'relic_type': 'normal', 'affixes': ['其他词条']}, condition)


def test_deepnight_editor_preserves_three_positive_negative_pairs():
    from PySide6.QtWidgets import QApplication
    from ui.pages.page_relics import EditRelicDialog, _vocabulary
    app = QApplication.instance() or QApplication([])
    positives = _vocabulary('deepnight')[:3]
    negatives = _vocabulary('deepnight', negative=True)[:3]
    relic = {'relic_type': 'deepnight', 'color': '蓝', 'name': '',
             'affixes': positives + negatives}
    dialog = EditRelicDialog(relic=relic)
    assert len(dialog.slots) == 3
    assert [slot['positive'] for slot in dialog.slots] == positives
    assert [slot['negative'] for slot in dialog.slots] == negatives
    assert len(dialog.values()[2]) == 6
    dialog.close()
    assert app is not None


def test_loadout_relics_follow_vessel_color_order():
    from types import SimpleNamespace
    from core.loadout_optimizer import optimize
    from core.relic_scoring import DEFAULT_SCORING

    vessel = {'hero_en': 'Wylder', 'name_en': 'Color order cup',
              'normal_en': 'Red, Green, Blue', 'deep_en': 'Yellow, Blue, Red'}
    relics = [dict(id=i, relic_type=mode, color=color, affixes=[])
              for i, mode, color in ((1, 'normal', '蓝'), (2, 'normal', '红'),
                                     (3, 'normal', '绿'), (4, 'deepnight', '红'),
                                     (5, 'deepnight', '黄'), (6, 'deepnight', '蓝'))]
    presets = SimpleNamespace(normal_general={'affixes': []},
                              deepnight_general={'affixes': []},
                              normal_dedicated={}, deepnight_whitelist_dedicated={})
    result = optimize({'hero': '追踪者'}, relics, [vessel], presets, DEFAULT_SCORING,
                      normal_preset_id='general', deepnight_preset_id='general',
                      max_alternatives=0, time_limit=2)
    assert [(item['relic_type'], item['color'], item['slot_index'])
            for item in result['relics']] == [
                ('normal', '红', 0), ('normal', '绿', 1), ('normal', '蓝', 2),
                ('deepnight', '黄', 0), ('deepnight', '蓝', 1), ('deepnight', '红', 2)]


def test_relic_page_inventory_optimizer_divider_can_move(monkeypatch, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from ui.pages import page_relics

    app = QApplication.instance() or QApplication([])
    inventory = RelicInventory(str(tmp_path / 'layout.sqlite3'))
    monkeypatch.setattr(page_relics, 'RelicInventory', lambda: inventory)

    class Presets:
        def get_dedicated_presets(self, mode):
            return {'own': {'name': '追踪者｜本职流派'},
                    'other': {'name': '女爵｜跨角色流派'}}

        def get_general_preset(self, mode):
            return {'affixes': []}

    page = page_relics.RelicPage(Presets())
    page.resize(1200, 800)
    page.show()
    app.processEvents()
    assert page.normal.findData('other') == -1
    assert page.deep.findData('other') == -1
    page.show_other_builds.setChecked(True)
    assert page.normal.findData('other') >= 0
    assert page.deep.findData('other') >= 0
    page.normal.setCurrentIndex(page.normal.findData('other'))
    page.deep.setCurrentIndex(page.deep.findData('other'))
    assert page.normal.currentData() == page.deep.currentData() == 'other'
    page.show_other_builds.setChecked(False)
    assert page.normal.findData('other') == -1
    assert page.deep.findData('other') == -1
    assert page.splitter.orientation() == Qt.Vertical
    assert page.splitter.count() == 2
    page.splitter.setSizes([440, 230])
    app.processEvents()
    assert page.splitter.sizes()[0] > page.splitter.sizes()[1]
    page.splitter.setSizes([190, 480])
    app.processEvents()
    assert page.splitter.sizes()[0] < page.splitter.sizes()[1]
    page.close()
