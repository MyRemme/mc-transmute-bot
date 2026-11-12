#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline recognition and run-loop regressions. No game input is sent."""
import os
import tempfile
import unittest
from unittest.mock import call, patch

import numpy as np
from PIL import Image

import mcb
from hotkeys import ChordTracker, key_identity, key_token, normalize


class HotkeyTests(unittest.TestCase):
    def test_letters_modifiers_and_mouse_normalization(self):
        self.assertEqual(normalize("Control + Shift + A"), "ctrl+shift+a")
        self.assertEqual(normalize("alt+mouse_x1"), "alt+mouse_x1")
        self.assertEqual(normalize("escape"), "esc")
        self.assertEqual(normalize("ctrl+plus"), "ctrl+vk_187")
        with self.assertRaises(ValueError):
            normalize("ctrl+imaginary_key")

    def test_combinations_repeat_and_release(self):
        tracker = ChordTracker({"pause": "ctrl+shift+p", "stop": "alt+mouse_x1"})
        self.assertEqual(tracker.update("left_ctrl", "ctrl", True), [])
        self.assertEqual(tracker.update("right_shift", "shift", True), [])
        self.assertEqual(tracker.update("p", "p", True), ["pause"])
        self.assertEqual(tracker.update("p", "p", True), [])
        tracker.update("p", "p", False)
        self.assertEqual(tracker.update("p", "p", True), ["pause"])
        tracker.update("p", "p", False)
        tracker.update("left_ctrl", "ctrl", False)
        tracker.update("right_shift", "shift", False)
        tracker.update("alt", "alt", True)
        self.assertEqual(tracker.update("side", "mouse_x1", True), ["stop"])

    def test_modifiers_on_both_sides_and_single_key(self):
        tracker = ChordTracker({"pause": "ctrl+p", "single": "p"})
        tracker.update("left", "ctrl", True)
        tracker.update("right", "ctrl", True)
        tracker.update("left", "ctrl", False)
        self.assertEqual(tracker.update("p", "p", True), ["pause"])
        self.assertEqual(tracker.update("right", "ctrl", False), [])

    def test_stop_chord_works_while_mouse_is_held(self):
        tracker = ChordTracker({"stop": "ctrl+f7"})
        tracker.update("mouse", "mouse_left", True)
        tracker.update("ctrl", "ctrl", True)
        self.assertEqual(tracker.update("f7", "f7", True), ["stop"])

    def test_control_character_uses_virtual_key(self):
        from pynput.keyboard import KeyCode
        self.assertEqual(key_token(KeyCode(vk=65, char="\x01")), "a")
        self.assertEqual(key_identity(KeyCode(vk=65, char="\x01")),
                         key_identity(KeyCode(vk=65, char="a")))

    def test_success_plays_bundled_wav_asynchronously(self):
        import winsound
        with patch.object(winsound, "PlaySound") as play:
            mcb.success_sound(mcb.DEFAULT_CFG)
        play.assert_called_once_with(os.path.join(mcb.HERE, "assets/success.wav"),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)

class VisionTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        self.icon = Image.fromarray(rng.integers(0, 256, (16, 16, 3), dtype=np.uint8))
        self.variants = mcb.build_variants(self.icon, auto_scale=False)
        self.targets = [("icon", self.variants[0], self.variants)]
        self.screen = Image.new("RGB", (96, 96), (139, 139, 139))
        self.screen.paste(self.icon, (40, 40))

    def test_grayscale_exact_missing_and_noisy(self):
        gray = mcb._to_gray(self.screen)
        template = mcb._to_gray(self.icon)
        score, pos = mcb.match(gray, template)
        self.assertGreater(score, 0.99)
        self.assertEqual(pos, (40, 40))
        noisy = gray + np.random.default_rng(0).normal(0, 6, gray.shape)
        self.assertGreater(mcb.match(noisy, template)[0], 0.9)
        self.assertLess(mcb.match(np.full_like(gray, 139), template)[0], 0.9)

    def test_rgb_location_ignores_grayscale_distractor(self):
        image = Image.new("RGB", (80, 32), (139, 139, 139))
        image.paste(self.icon.convert("L").convert("RGB"), (0, 0))
        image.paste(self.icon, (50, 8))
        result = mcb.scan(mcb._to_gray(image), self.targets, 0.9,
                          rgb=np.asarray(image, dtype=np.float32))
        self.assertGreater(result[0], 0.99)
        self.assertEqual(result[2], (50, 8))

    def test_fft_scores_match_direct_correlation(self):
        rng = np.random.default_rng(23)
        for H, W, h, w in [(13, 17, 5, 6), (16, 16, 4, 4), (9, 17, 9, 17)]:
            for channels in (1, 3):
                for masked in (False, True):
                    shape = (H, W) if channels == 1 else (H, W, 3)
                    template_shape = (h, w) if channels == 1 else (h, w, 3)
                    image = rng.integers(0, 256, shape).astype(np.float32)
                    template = rng.integers(0, 256, template_shape).astype(np.float32)
                    mask = rng.random((h, w)) > 0.3 if masked else np.ones((h, w), bool)
                    scores = np.array([
                        [np.corrcoef(image[y:y + h, x:x + w][mask].ravel(),
                                     template[mask].ravel())[0, 1] for x in range(W - w + 1)]
                        for y in range(H - h + 1)])
                    y, x = np.unravel_index(int(scores.argmax()), scores.shape)
                    with self.subTest(size=(H, W, h, w), channels=channels, masked=masked):
                        score, pos = mcb._match_exact(image, template, mask)
                        self.assertEqual(pos, (x, y))
                        self.assertAlmostEqual(score, scores[y, x], places=5)

    def test_template_cache_tracks_pixels_and_mask_changes(self):
        image = np.asarray(self.screen, dtype=np.float32)
        template = np.asarray(self.icon, dtype=np.float32).copy()
        mask = np.ones(template.shape[:2], bool)
        self.assertGreater(mcb._match_exact(image, template, mask)[0], 0.99)
        template[mask] = 0
        self.assertEqual(mcb._match_exact(image, template, mask)[0], 0)
        mask[:] = False
        self.assertEqual(mcb._match_exact(image, template, mask)[0], 0)

    def test_transparent_background(self):
        icon = Image.new("RGBA", (24, 24), (255, 0, 255, 0))
        icon.paste(self.icon, (4, 4))
        variants = mcb.build_variants(icon, auto_scale=False)
        mask = variants[0][3]
        self.assertEqual(int(mask.sum()), 16 * 16)
        screen = Image.new("RGB", (48, 48), (0, 255, 0))
        screen.paste(icon, (12, 12), icon)
        score = mcb.scan(mcb._to_gray(screen), [("alpha", variants[0], variants)], 0.9,
                         rgb=np.asarray(screen, dtype=np.float32))[0]
        self.assertGreater(score, 0.99)

    def test_distinct_colors_are_loaded_as_separate_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            self.icon.save(os.path.join(directory, "a.png"))
            self.icon.convert("L").convert("RGB").save(os.path.join(directory, "b.png"))
            with patch.object(mcb, "TARGET_DIR", directory):
                self.assertEqual(len(mcb.load_targets()), 2)

    def test_scaled_template_larger_than_capture(self):
        icon = self.icon.resize((128, 128), Image.Resampling.NEAREST)
        variants = mcb.build_variants(icon)
        screen = Image.new("RGB", (96, 96), (139, 139, 139))
        screen.paste(icon.resize((64, 64), Image.Resampling.BOX), (16, 16))
        result = mcb.scan(mcb._to_gray(screen), [("large", variants[0], variants)], 0.9,
                          rgb=np.asarray(screen, dtype=np.float32))
        self.assertGreater(result[0], 0.99)
        self.assertEqual(result[3], (64, 64))

    def test_initial_left_result_is_clicked_once_and_loop_stops(self):
        slots = [(100, 100), (200, 100), (300, 100)]
        blank = Image.new("RGB", (96, 96), (139, 139, 139))

        def capture(cx, cy, half=48):
            image = self.screen if (cx, cy) == tuple(slots[2]) else blank
            return image, (cx - half, cy - half)

        cfg = {**mcb.DEFAULT_CFG, "slots": slots}
        state = {"stop": False, "paused": False}
        with patch.object(mcb, "client_box", return_value=None), \
                patch.object(mcb, "game_ready", return_value=(True, "")), \
                patch.object(mcb, "load_macro", return_value=None), \
                patch.object(mcb, "grab_patch", side_effect=capture), \
                patch.object(mcb, "success_sound"), patch.object(mcb, "click_at") as click:
            result = mcb.run_auto(cfg, self.targets, state, {"passes": 5}, on_log=lambda _: None)
        self.assertEqual(result["hits"], 1)
        self.assertEqual(result["rounds"], 1)
        click.assert_called_once_with(300, 100)

    def test_recorded_loop_detects_and_stops(self):
        cfg = {**mcb.DEFAULT_CFG, "watch": [0, 0, 96, 96], "settle": 0}
        state = {"stop": False, "paused": False}
        frame = (mcb._to_gray(self.screen), self.screen)
        with patch.object(mcb, "client_box", return_value=None), \
                patch.object(mcb, "game_ready", return_value=(True, "")), \
                patch.object(mcb, "play_events", return_value=True), \
                patch.object(mcb, "grab", return_value=frame), \
                patch.object(mcb, "success_sound"), patch.object(mcb, "_take") as take:
            result = mcb.run_loop(cfg, self.targets, {"spin": []}, state,
                                  {"passes": 5}, on_log=lambda _: None)
        self.assertEqual(result["hits"], 1)
        self.assertEqual(result["rounds"], 1)
        take.assert_called_once()

    def test_auto_dry_run_never_clicks(self):
        cfg = {**mcb.DEFAULT_CFG, "slots": [(48, 48), (48, 148), (48, 248)]}
        state = {"stop": False, "paused": False}
        with patch.object(mcb, "client_box", return_value=None), \
                patch.object(mcb, "game_ready", return_value=(True, "")), \
                patch.object(mcb, "load_macro", return_value=None), \
                patch.object(mcb, "grab_patch", return_value=(self.screen, (0, 0))), \
                patch.object(mcb, "beep"), patch.object(mcb, "click_at") as click:
            result = mcb.run_auto(cfg, self.targets, state,
                                  {"passes": 5, "dry": True}, on_log=lambda _: None)
        self.assertEqual(result["hits"], 1)
        click.assert_not_called()


class SavedScreenshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = os.path.join(mcb.HERE, "tests", "fixtures")
        cls.references = []
        for name, filename, box in [
                ("green", "user_table.png", (244, 243, 293, 291)),
                ("gray", "live_inventory_reference.png", (24, 23, 67, 71))]:
            with Image.open(os.path.join(cls.fixture_dir, filename)) as source:
                variants = mcb.build_variants(source.convert("RGB").crop(box))
            cls.references.append((name, variants[0], variants))
        cls.targets = cls.references

    def test_all_saved_target_sizes(self):
        for name, _native, variants in self.targets:
            for h, w, _gray, _mask, rgb in variants:
                if max(h, w) > 96:
                    continue
                with self.subTest(target=name, size=(w, h)):
                    image = Image.new("RGB", (96, 96), (139, 139, 139))
                    x, y = (96 - w) // 2, (96 - h) // 2
                    image.paste(Image.fromarray(rgb.astype(np.uint8)), (x, y))
                    with patch.object(mcb, "grab_patch", return_value=(image, (0, 0))):
                        score, _name, pos, size = mcb.scan_at_slots(
                            self.targets, [(48, 48)], 0.9)
                    self.assertGreaterEqual(score, 0.9)
                    self.assertLessEqual(abs(pos[0] + size[1] // 2 - 48), 4)
                    self.assertLessEqual(abs(pos[1] + size[0] // 2 - 48), 4)

    def test_saved_target_on_different_background(self):
        for name, native, _variants in self.targets:
            h, w, _gray, mask, rgb = native
            foreground = rgb.copy()
            foreground[~mask] = (139, 139, 139)
            image = Image.new("RGB", (96, 96), (139, 139, 139))
            image.paste(Image.fromarray(foreground.astype(np.uint8)), (16, 16))
            with self.subTest(target=name), \
                    patch.object(mcb, "grab_patch", return_value=(image, (0, 0))):
                result = mcb.scan_at_slots(self.targets, [(48, 48)], 0.9)
                self.assertGreater(result[0], 0.99)
                self.assertEqual(result[2], (16, 16))

    def test_wrong_items_in_saved_game_frame(self):
        path = os.path.join(self.fixture_dir, "last_watch.png")
        if not os.path.exists(path):
            self.skipTest("No saved game frame")
        with Image.open(path) as source:
            frame = source.convert("RGB")
        for x, y in [(114, 76), (114, 130), (114, 188), (268, 272)]:
            with self.subTest(slot=(x, y)):
                image = frame.crop((x - 48, y - 48, x + 48, y + 48))
                result = mcb.scan(mcb._to_gray(image), self.targets, 0.9,
                                  rgb=np.asarray(image, dtype=np.float32))
                self.assertLess(result[0], 0.9)

    def test_new_references_match_across_backgrounds(self):
        for name, _native, variants in self.references:
            targets = [(name, variants[0], variants)]
            for other_name, other, _variants in self.references:
                if name == other_name:
                    continue
                with self.subTest(template=name, reference=other_name):
                    image = Image.new("RGB", (96, 96), (139, 139, 139))
                    image.paste(Image.fromarray(other[4].astype(np.uint8)), (24, 24))
                    result = mcb.scan(mcb._to_gray(image), targets, 0.9,
                                      rgb=np.asarray(image, dtype=np.float32))
                    self.assertGreater(result[0], 0.95)

    def test_supplied_results_contain_no_target(self):
        with Image.open(os.path.join(self.fixture_dir, "user_results.png")) as source:
            image = source.convert("RGB")
        result = mcb.scan(mcb._to_gray(image), self.targets, 0.9,
                          rgb=np.asarray(image, dtype=np.float32))
        self.assertLess(result[0], 0.9)

    def test_only_left_three_are_scanned_then_correct_result_clicked(self):
        with Image.open(os.path.join(self.fixture_dir, "user_table.png")) as source:
            initial = source.convert("RGB")
        slots = [(112, 69), (112, 125), (112, 181)]
        bottom = (268, 267)
        # Use pixels from the complete table screenshot, independently of the templates.
        item = initial.crop((244, 243, 293, 291))
        for row, (cx, cy) in enumerate(slots):
            with self.subTest(row=row + 1):
                current = [initial.copy()]
                scanned = []

                def capture(x, y, half=48):
                    scanned.append((x, y))
                    return (current[0].crop((x - half, y - half, x + half, y + half)),
                            (x - half, y - half))

                def select(x, y):
                    if len(click.call_args_list) == 1:
                        current[0].paste(item, (cx - 24, cy - 24))

                cfg = {**mcb.DEFAULT_CFG, "slots": slots + [bottom]}
                state = {"stop": False, "paused": False}
                with patch.object(mcb, "client_box", return_value=None), \
                        patch.object(mcb, "game_ready", return_value=(True, "")), \
                        patch.object(mcb, "grab_patch", side_effect=capture), \
                        patch.object(mcb.time, "sleep"), \
                        patch.object(mcb, "success_sound"), \
                        patch.object(mcb, "click_at", side_effect=select) as click:
                    result = mcb.run_auto(cfg, self.references, state, {"passes": 5},
                                          on_log=lambda _: None)
                self.assertEqual(result["rounds"], 2)
                self.assertEqual(result["hits"], 1)
                self.assertNotIn(bottom, scanned)
                self.assertEqual(len(scanned), 6)
                self.assertEqual(click.call_count, 2)
                self.assertEqual(click.call_args_list[0], call(*slots[0]))
                hit_x, hit_y = click.call_args_list[1].args
                self.assertLess(abs(hit_x - cx), 8)
                self.assertLess(abs(hit_y - cy), 8)

    def test_auto_dry_miss_does_not_refresh(self):
        image = Image.new("RGB", (96, 96), (139, 139, 139))
        cfg = {**mcb.DEFAULT_CFG, "slots": [(100, 100), (100, 156), (100, 212)]}
        state = {"stop": False, "paused": False}
        with patch.object(mcb, "client_box", return_value=None), \
                patch.object(mcb, "game_ready", return_value=(True, "")), \
                patch.object(mcb, "grab_patch", return_value=(image, (0, 0))), \
                patch.object(mcb.time, "sleep"), patch.object(mcb, "click_at") as click:
            result = mcb.run_auto(cfg, self.targets, state, {"passes": 2, "dry": True},
                                  on_log=lambda _: None)
        self.assertEqual(result["hits"], 0)
        self.assertEqual(result["rounds"], 2)
        click.assert_not_called()

    def test_real_game_hit_and_transmuted_result(self):
        paths = [os.path.join(self.fixture_dir, filename)
                 for filename in ("live_hit_before.png", "live_hit_after.png")]
        if not all(os.path.exists(path) for path in paths):
            self.skipTest("No live collection screenshots")
        with Image.open(paths[0]) as source:
            before = source.convert("RGB")
        with Image.open(paths[1]) as source:
            after = source.convert("RGB")
        for frame, center in [(before, (114, 130)), (after, (270, 272))]:
            x, y = center
            image = frame.crop((x - 48, y - 48, x + 48, y + 48))
            targets = self.references
            if frame is after:
                # The converted stack has a quantity label covering the lower icon.
                targets = []
                for name, native, _variants in self.references:
                    h, w, gray, mask, rgb = native
                    mask = mask.copy()
                    mask[-14:] = False
                    variant = (h, w, gray, mask, rgb)
                    targets.append((name, variant, [variant]))
            result = mcb.scan(mcb._to_gray(image), targets, 0.9,
                              rgb=np.asarray(image, dtype=np.float32))
            self.assertGreater(result[0], 0.95)
        image = after.crop((66, 25, 162, 233))
        result = mcb.scan(mcb._to_gray(image), self.targets, 0.9,
                          rgb=np.asarray(image, dtype=np.float32))
        self.assertLess(result[0], 0.9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
