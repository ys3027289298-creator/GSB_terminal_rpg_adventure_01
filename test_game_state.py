"""Tests for the unified game state model: quests, inventory capacity,
equipment slots, enemy scaling and save/load recovery."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from character import Character
from combat import CombatSystem
from quests import Quest, QuestManager


def make_player():
    return Character("Hero", "warrior")


def quest_by_name(manager, name):
    for quests in manager.all_quests.values():
        for quest in quests:
            if quest.name == name:
                return quest
    raise KeyError(name)


def quest_snapshot(player):
    active = sorted(
        (QuestManager._quest_name(q),
         q.current_progress, q.completed)
        for q in player.quests
    )
    completed = sorted(QuestManager._quest_name(q) for q in player.completed_quests)
    return active, completed


def inventory_snapshot(player):
    return {
        'gold': player.gold,
        'inventory': [dict(item) for item in player.inventory],
        'equipped_weapon': dict(player.equipped_weapon) if player.equipped_weapon else None,
        'equipped_armor': dict(player.equipped_armor) if player.equipped_armor else None,
    }


def save_data(path):
    with open(path, 'r') as handle:
        return json.load(handle)


class QuestStateTests(unittest.TestCase):
    def setUp(self):
        self.player = make_player()
        self.manager = QuestManager()

    def available_names(self):
        return [q.name for q in self.manager.get_available_quests(self.player)]

    def test_quest_cannot_be_accepted_twice(self):
        accepted_names = []
        while True:
            available = self.manager.get_available_quests(self.player)
            if not available:
                break
            quest = available[0]
            self.assertTrue(self.manager.assign_quest(self.player, 0))
            accepted_names.append(quest.name)
            # Once accepted it must disappear from offers immediately.
            self.assertNotIn(quest.name, self.available_names())

        self.assertEqual(len(self.player.quests), len(accepted_names))
        self.assertEqual(len(self.player.quests), len(set(accepted_names)))
        self.assertIn("First Blood", accepted_names)

    def test_prerequisite_blocks_quest(self):
        # Orc Slayer is visible at level 3 but requires First Blood.
        self.player.level = 3
        self.assertNotIn("Orc Slayer", self.available_names())
        available = self.manager.get_available_quests(self.player)
        orc_index = None
        for index, quest in enumerate(available):
            if quest.name == "Orc Slayer":
                orc_index = index
        self.assertIsNone(orc_index)

        # Completing the prerequisite unlocks the quest.
        first_blood = quest_by_name(self.manager, "First Blood")
        first_blood.completed = True
        self.player.completed_quests.append(first_blood)
        self.assertIn("Orc Slayer", self.available_names())

    def test_cross_quest_prerequisite_chain(self):
        # The Mysterious Village -> The Ancient Prophecy
        village = quest_by_name(self.manager, "The Mysterious Village")
        prophecy = quest_by_name(self.manager, "The Ancient Prophecy")

        self.assertNotIn(prophecy.name, self.available_names())

        self.player.quests.append(village)
        self.assertNotIn(prophecy.name, self.available_names())

        self.player.quests.remove(village)
        village.completed = True
        self.player.completed_quests.append(village)
        self.manager.story_progress = 1
        self.assertIn(prophecy.name, self.available_names())

        # Prerequisites are enforced at accept time as well.
        prophecy.prerequisites = ["Does Not Exist"]
        available = self.manager.get_available_quests(self.player)
        self.assertNotIn(prophecy.name, available)

    def test_completed_quest_progresses_after_load(self):
        first_blood = quest_by_name(self.manager, "First Blood")
        self.player.quests.append(first_blood)
        self.manager.update_quest_progress(self.player, "kill", "goblin")
        self.assertEqual(first_blood.current_progress, 1)
        self.assertFalse(first_blood.completed)

        # Reload and continue: restored quest must keep progressing.
        data = first_blood.to_dict()
        restored_manager = QuestManager()
        self.player.quests = [data]
        restored_manager.restore_player_state(self.player)

        restored_quest = self.player.quests[0]
        self.assertIs(restored_quest, quest_by_name(restored_manager, "First Blood"))
        restored_manager.update_quest_progress(self.player, "kill", "goblin")
        restored_manager.update_quest_progress(self.player, "kill", "goblin")
        self.assertTrue(restored_quest.completed)
        self.assertEqual([], self.player.quests)
        self.assertIn(restored_quest, self.player.completed_quests)


class InventoryEquipmentTests(unittest.TestCase):
    def setUp(self):
        self.player = make_player()

    def test_inventory_capacity_blocks_pickup(self):
        weapon = {'name': 'Rusty Dagger', 'type': 'weapon', 'damage': 3,
                  'description': 'A worn dagger'}
        for _ in range(Character.INVENTORY_CAPACITY):
            self.assertTrue(self.player.add_item(dict(weapon)))

        self.assertEqual(len(self.player.inventory), Character.INVENTORY_CAPACITY)
        self.assertFalse(self.player.add_item(weapon))
        self.assertEqual(len(self.player.inventory), Character.INVENTORY_CAPACITY)

    def test_full_inventory_skips_quest_reward(self):
        player = self.player
        for index in range(Character.INVENTORY_CAPACITY):
            player.add_item({'name': f'Junk {index}', 'type': 'misc',
                             'description': 'junk'})

        quest = Quest("Q", "desc", "kill_goblin", target="goblin",
                      target_amount=1,
                      reward_items=[{'name': 'Health Potion', 'type': 'consumable',
                                     'heal': 30, 'description': 'Restores 30 HP'}])
        player.quests.append(quest)
        quest.completed = True

        manager = QuestManager()
        manager.complete_quest(player, quest)

        self.assertEqual(len(player.inventory), Character.INVENTORY_CAPACITY)
        self.assertNotIn('Health Potion', [i['name'] for i in player.inventory])

    def test_same_equipment_cannot_be_equipped_twice(self):
        weapon = {'name': 'Iron Sword', 'type': 'weapon', 'damage': 8,
                  'description': 'A sturdy iron blade'}
        self.player.inventory.append(weapon)
        self.player.inventory.remove(weapon)

        self.assertTrue(self.player.equip_weapon(weapon))
        self.assertIs(self.player.equipped_weapon, weapon)

        # Equipping the already-worn item must not duplicate it.
        self.assertFalse(self.player.equip_weapon(weapon))
        self.assertIs(self.player.equipped_weapon, weapon)
        self.assertEqual(self.player.inventory.count(weapon), 0)

        # Swapping moves the old piece back into inventory exactly once.
        better = {'name': 'Silver Sword', 'type': 'weapon', 'damage': 15,
                  'description': 'A well-crafted silver blade'}
        self.player.inventory.append(better)
        self.player.inventory.remove(better)
        self.assertTrue(self.player.equip_weapon(better))
        self.assertIs(self.player.equipped_weapon, better)
        self.assertEqual(self.player.inventory, [weapon])


class EnemyScalingTests(unittest.TestCase):
    def test_scaling_matches_player_level_exactly(self):
        level_one = CombatSystem.create_enemy('goblin', 1)
        self.assertEqual(level_one.level, 1)
        self.assertEqual(level_one.max_health, 30)
        self.assertEqual(level_one.attack, 8)
        self.assertEqual(level_one.defense, 2)

        # round() must be used, not int(): 30 * 1.9 ~= 57, exp 25 * 1.9 ~= 48.
        level_four = CombatSystem.create_enemy('goblin', 4)
        self.assertEqual(level_four.level, 4)
        self.assertEqual(level_four.max_health, 57)
        self.assertEqual(level_four.exp_reward, 48)

        level_ten = CombatSystem.create_enemy('goblin', 10)
        self.assertEqual(level_ten.max_health, 111)

    def test_scaling_is_monotonic_and_bounded(self):
        previous = None
        for level in range(1, 16):
            enemy = CombatSystem.create_enemy('dragon', level)
            self.assertGreaterEqual(enemy.max_health, 200)
            self.assertGreaterEqual(enemy.attack, 25)
            if previous is not None:
                self.assertGreaterEqual(enemy.max_health, previous.max_health)
            previous = enemy

        # Levels below 1 must clamp instead of shrinking enemies.
        self.assertEqual(CombatSystem.create_enemy('goblin', 0).max_health, 30)


class SaveLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.tmpdir)

        self.player = make_player()
        self.manager = QuestManager()

    def tearDown(self):
        os.chdir(self.original_cwd)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _populate_state(self):
        first_blood = quest_by_name(self.manager, "First Blood")
        village = quest_by_name(self.manager, "The Mysterious Village")
        self.player.quests.extend([first_blood, village])
        self.manager.update_quest_progress(self.player, "kill", "goblin")

        treasure = quest_by_name(self.manager, "Treasure Hunter")
        treasure.completed = True
        self.player.completed_quests.append(treasure)

        sword = {'name': 'Iron Sword', 'type': 'weapon', 'damage': 8,
                 'description': 'A sturdy iron blade'}
        armor = {'name': 'Leather Armor', 'type': 'armor', 'defense': 5,
                 'description': 'Basic leather protection'}
        potion = {'name': 'Health Potion', 'type': 'consumable', 'heal': 30,
                  'description': 'Restores 30 HP'}
        self.player.add_item(sword)
        self.player.add_item(potion)
        self.player.inventory.remove(sword)
        self.player.equip_weapon(sword)
        self.player.add_item(armor)
        self.player.inventory.remove(armor)
        self.player.equip_armor(armor)

    def test_save_then_load_preserves_quest_and_inventory_state(self):
        self._populate_state()
        before_quests = quest_snapshot(self.player)
        before_bag = inventory_snapshot(self.player)

        save_path = os.path.join(self.tmpdir, 'hero.sav')
        self.player.save_to_file(save_path)

        loaded = Character.load_from_file(save_path)
        QuestManager().restore_player_state(loaded)

        self.assertEqual(before_quests, quest_snapshot(loaded))
        self.assertEqual(before_bag, inventory_snapshot(loaded))
        self.assertEqual(save_data(save_path)['quests'][0]['current_progress'], 1)

    def test_completed_quests_do_not_reopen_after_load(self):
        self._populate_state()
        save_path = os.path.join(self.tmpdir, 'hero.sav')
        self.player.save_to_file(save_path)

        loaded = Character.load_from_file(save_path)
        new_manager = QuestManager()
        new_manager.restore_player_state(loaded)

        names = [q.name for q in new_manager.get_available_quests(loaded)]
        self.assertNotIn("Treasure Hunter", names)
        self.assertNotIn("First Blood", names)
        self.assertNotIn("The Mysterious Village", names)
        # The Ancient Prophecy stays locked until its prerequisite completes.
        self.assertNotIn("The Ancient Prophecy", names)

        completed = quest_by_name(new_manager, "Treasure Hunter")
        self.assertTrue(completed.completed)

    def test_consecutive_save_load_cycles_are_stable(self):
        self._populate_state()
        first_path = os.path.join(self.tmpdir, 'first.sav')
        second_path = os.path.join(self.tmpdir, 'second.sav')

        self.player.save_to_file(first_path)
        with open(first_path, 'rb') as handle:
            first_bytes = handle.read()

        for _ in range(3):
            loaded = Character.load_from_file(first_path)
            manager = QuestManager()
            manager.restore_player_state(loaded)
            loaded.save_to_file(second_path)

        with open(second_path, 'rb') as handle:
            self.assertEqual(first_bytes, handle.read())

        # State also stays identical semantically across the cycles.
        final = Character.load_from_file(second_path)
        QuestManager().restore_player_state(final)
        self.assertEqual(quest_snapshot(self.player), quest_snapshot(final))
        self.assertEqual(inventory_snapshot(self.player), inventory_snapshot(final))

    def test_crash_during_save_keeps_previous_save_loadable(self):
        self._populate_state()
        save_path = os.path.join(self.tmpdir, 'hero.sav')
        self.player.save_to_file(save_path)
        with open(save_path, 'rb') as handle:
            good_bytes = handle.read()
        good_state = save_data(save_path)

        # Simulate a crash halfway through serializing the next save.
        def broken_dump(*args, **kwargs):
            args[1].write('{"name": "Hero", "quests": [')  # truncated, unusable
            raise RuntimeError("simulated crash mid-save")

        with mock.patch('character.json.dump', side_effect=broken_dump):
            with self.assertRaises(RuntimeError):
                self.player.save_to_file(save_path)

        # The previous good save must be intact and fully loadable.
        with open(save_path, 'rb') as handle:
            self.assertEqual(handle.read(), good_bytes)
        recovered = Character.load_from_file(save_path)
        QuestManager().restore_player_state(recovered)
        self.assertEqual(good_state['quests'], save_data(save_path)['quests'])
        self.assertEqual(quest_snapshot(self.player), quest_snapshot(recovered))
        self.assertEqual(inventory_snapshot(self.player), inventory_snapshot(recovered))
        self.assertFalse(os.path.exists(save_path + '.tmp'))


if __name__ == '__main__':
    unittest.main()
