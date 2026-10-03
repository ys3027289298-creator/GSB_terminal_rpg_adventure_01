"""Tests for the unified state model: quests, inventory, equipment,
enemy scaling and save/load recovery."""
import json
import os
import shutil
import tempfile
import unittest

from character import Character
from combat import CombatSystem
from quests import QuestManager


def snapshot(player):
    """Capture quest + inventory state for equality checks across reloads."""
    return {
        'inventory': json.loads(json.dumps(player.inventory)),
        'equipped_weapon': player.equipped_weapon,
        'equipped_armor': player.equipped_armor,
        'gold': player.gold,
        'level': player.level,
        'active_quests': sorted(
            (q.name, q.current_progress, q.completed) for q in player.quests),
        'completed_quests': sorted(q.name for q in player.completed_quests),
    }


class StateModelTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.save_file = os.path.join(self.tmpdir, 'hero.sav')
        self.player = Character('Hero', 'warrior')
        self.qm = QuestManager()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def reload(self, player=None, save_file=None):
        """Save, simulate a process restart, load and re-sync state."""
        player = player or self.player
        player.save_to_file(save_file or self.save_file)
        fresh_qm = QuestManager()  # new "process": no shared quest objects
        loaded = Character.load_from_file(save_file or self.save_file)
        fresh_qm.sync_player(loaded)
        return loaded, fresh_qm

    def accept(self, qm, player, name):
        available = qm.get_available_quests(player.level, player)
        index = next((i for i, q in enumerate(available) if q.name == name), None)
        self.assertIsNotNone(index, f'{name} not offered')
        self.assertTrue(qm.assign_quest(player, index))

    def complete_quest(self, qm, player, name, action, target, times):
        self.accept(qm, player, name)
        for _ in range(times):
            qm.update_quest_progress(player, action, target)
        self.assertIn(name, [q.name for q in player.completed_quests])


class TestQuestAcceptance(StateModelTestCase):
    def test_duplicate_acceptance_rejected(self):
        self.accept(self.qm, self.player, 'First Blood')
        self.assertEqual(len(self.player.quests), 1)
        # active quest is no longer offered, so it cannot be re-accepted
        names = [q.name for q in self.qm.get_available_quests(
            self.player.level, self.player)]
        self.assertNotIn('First Blood', names)
        self.assertEqual(len(self.player.quests), 1)

    def test_completed_quest_cannot_be_reaccepted(self):
        self.complete_quest(self.qm, self.player, 'First Blood', 'kill', 'goblin', 3)
        names = [q.name for q in self.qm.get_available_quests(
            self.player.level, self.player)]
        self.assertNotIn('First Blood', names)

    def test_cross_quest_prerequisites(self):
        self.player.level = 3  # unlock intermediate tier
        # Orc Slayer requires First Blood
        names = [q.name for q in self.qm.get_available_quests(3, self.player)]
        self.assertNotIn('Orc Slayer', names)
        self.complete_quest(self.qm, self.player, 'First Blood', 'kill', 'goblin', 3)
        # now the chain unlocks one step at a time
        self.accept(self.qm, self.player, 'Orc Slayer')
        names = [q.name for q in self.qm.get_available_quests(3, self.player)]
        self.assertNotIn('Cave Explorer', names)
        # story chain: prophecy requires the village mystery
        names = [q.name for q in self.qm.get_available_quests(3, self.player)]
        self.assertNotIn('The Ancient Prophecy', names)

    def test_prerequisites_survive_reload(self):
        self.player.level = 3
        self.complete_quest(self.qm, self.player, 'First Blood', 'kill', 'goblin', 3)
        loaded, qm2 = self.reload()
        # completed prerequisite is still known after restart
        self.accept(qm2, loaded, 'Orc Slayer')
        # but the next link in the chain is still locked
        names = [q.name for q in qm2.get_available_quests(3, loaded)]
        self.assertNotIn('Cave Explorer', names)


class TestInventoryAndEquipment(StateModelTestCase):
    def test_inventory_capacity_enforced(self):
        for i in range(Character.INVENTORY_CAPACITY):
            self.assertTrue(self.player.add_item(
                {'name': f'Item {i}', 'type': 'misc', 'description': ''}))
        self.assertFalse(self.player.add_item(
            {'name': 'Overflow', 'type': 'misc', 'description': ''}))
        self.assertEqual(len(self.player.inventory), Character.INVENTORY_CAPACITY)

    def test_no_duplicate_equip(self):
        sword = {'name': 'Sword', 'type': 'weapon', 'damage': 5, 'description': ''}
        self.player.add_item(sword)
        self.assertTrue(self.player.equip_weapon(sword))
        self.assertNotIn(sword, self.player.inventory)
        # equipping the same weapon again must not duplicate it
        self.assertFalse(self.player.equip_weapon(sword))
        self.assertEqual(self.player.inventory, [])
        self.assertIs(self.player.equipped_weapon, sword)

    def test_equip_swap_with_full_inventory(self):
        sword_a = {'name': 'A', 'type': 'weapon', 'damage': 1, 'description': ''}
        sword_b = {'name': 'B', 'type': 'weapon', 'damage': 2, 'description': ''}
        self.assertTrue(self.player.equip_weapon(sword_a))
        for i in range(Character.INVENTORY_CAPACITY):
            self.player.add_item({'name': f'Junk {i}', 'type': 'misc', 'description': ''})
        self.player.inventory.pop()  # make room to hold sword_b before equip
        self.player.inventory.append(sword_b)
        # swap succeeds even when full: b leaves inventory, a takes its slot
        self.assertTrue(self.player.equip_weapon(sword_b))
        self.assertIs(self.player.equipped_weapon, sword_b)
        self.assertIn(sword_a, self.player.inventory)
        self.assertNotIn(sword_b, self.player.inventory)


class TestEnemyScaling(unittest.TestCase):
    def test_level_one_is_base_stats(self):
        enemy = CombatSystem.create_enemy('goblin', 1)
        self.assertEqual(enemy.max_health, 30)
        self.assertEqual(enemy.attack, 8)

    def test_scaling_rounds_not_truncates(self):
        enemy = CombatSystem.create_enemy('goblin', 10)
        self.assertEqual(enemy.max_health, 111)  # 30 * 3.7, not 110
        enemy = CombatSystem.create_enemy('goblin', 2)
        self.assertEqual(enemy.gold_reward, 20)  # 15 * 1.3 = 19.5 -> 20

    def test_scaling_monotonic_and_clamped(self):
        low = CombatSystem.create_enemy('orc', 0)  # clamped to level 1
        self.assertEqual(low.max_health, 60)
        previous = 0
        for level in range(1, 12):
            hp = CombatSystem.create_enemy('troll', level).max_health
            self.assertGreater(hp, previous)
            previous = hp

    def test_loot_not_shared_with_template(self):
        first = CombatSystem.create_enemy('goblin', 1)
        first.loot[0]['damage'] = 999
        second = CombatSystem.create_enemy('goblin', 1)
        self.assertEqual(second.loot[0]['damage'], 3)


class TestSaveLoadRecovery(StateModelTestCase):
    def build_rich_state(self):
        self.player.level = 3
        self.complete_quest(self.qm, self.player, 'First Blood', 'kill', 'goblin', 3)
        self.accept(self.qm, self.player, 'Treasure Hunter')
        self.qm.update_quest_progress(self.player, 'gold_gained', None, 40)
        self.player.add_item({'name': 'Iron Sword', 'type': 'weapon',
                              'damage': 8, 'description': 'blade'})
        self.player.add_item({'name': 'Health Potion', 'type': 'consumable',
                              'heal': 30, 'description': 'heals'})
        self.player.equip_weapon({'name': 'Rusty Dagger', 'type': 'weapon',
                                  'damage': 3, 'description': 'worn'})

    def test_crash_midgame_then_recover(self):
        self.build_rich_state()
        before = snapshot(self.player)
        # save succeeds even with active quests (previously crashed here)
        self.player.save_to_file(self.save_file)
        # simulate crash: brand new process state, load from disk only
        loaded, qm2 = self.reload()
        self.assertEqual(snapshot(loaded), before)
        # quest objects are the canonical ones again (progress keeps tracking)
        self.assertIs(loaded.quests[0], qm2.find_quest('Treasure Hunter'))
        qm2.update_quest_progress(loaded, 'gold_gained', None, 60)
        self.assertIn('Treasure Hunter',
                      [q.name for q in loaded.completed_quests])

    def test_repeated_save_load_cycles_are_stable(self):
        self.build_rich_state()
        expected = snapshot(self.player)
        player, qm = self.player, self.qm
        for _ in range(3):
            player, qm = self.reload(player)
            self.assertEqual(snapshot(player), expected)

    def test_corrupted_save_does_not_crash_loader(self):
        self.build_rich_state()
        self.player.save_to_file(self.save_file)
        # simulate a crash mid-write: truncated file
        with open(self.save_file, 'r+') as handle:
            handle.truncate(os.path.getsize(self.save_file) // 2)
        with self.assertRaises((json.JSONDecodeError, ValueError)):
            Character.load_from_file(self.save_file)

    def test_save_format_keys_unchanged(self):
        self.build_rich_state()
        self.player.save_to_file(self.save_file)
        with open(self.save_file) as handle:
            data = json.load(handle)
        self.assertEqual(set(data), {
            'name', 'character_class', 'level', 'experience',
            'experience_to_next_level', 'max_health', 'current_health',
            'strength', 'magic', 'defense', 'agility', 'gold', 'inventory',
            'equipped_weapon', 'equipped_armor', 'quests', 'completed_quests'})


if __name__ == '__main__':
    unittest.main()
