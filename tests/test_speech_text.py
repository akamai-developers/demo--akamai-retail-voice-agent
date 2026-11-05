import unittest

from retail_voice.speech_text import number_words, speech_chunks, spoken


class SpokenFormTests(unittest.TestCase):
    def test_prices_and_counts_become_words(self):
        self.assertEqual(
            spoken("The Harbor Deck Jacket is $119, so it stays under $125."),
            "The Harbor Deck Jacket is one hundred nineteen dollars, so it stays under one "
            "hundred twenty five dollars.",
        )
        self.assertEqual(
            spoken("$69.00 and $12,345.67"),
            "sixty nine dollars and twelve thousand "
            "three hundred forty five dollars and sixty seven cents",
        )
        self.assertEqual(
            spoken("navy, size M, is in stock (7 available)."),
            "navy, size medium, is in stock (seven available).",
        )
        self.assertEqual(spoken("A 700-fill down jacket"), "A seven hundred-fill down jacket")
        self.assertEqual(spoken("$1"), "one dollar")

    def test_ordinals_become_words(self):
        self.assertEqual(
            spoken("The 2nd one is $89 and the 1st is $69."),
            "The second one is eighty nine dollars and the first is sixty nine dollars.",
        )
        self.assertEqual(
            spoken("3rd, 4th, 12th, 20th, 21st, 100th"),
            "third, fourth, twelfth, twentieth, twenty first, one hundredth",
        )
        self.assertEqual(spoken("size 2XL, about 5k"), "size 2XL, about 5k")

    def test_ids_and_versions_are_left_alone(self):
        self.assertEqual(spoken("jacket-b costs $5"), "jacket-b costs five dollars")
        self.assertEqual(spoken("catalog 2026.11.1"), "catalog 2026.11.1")

    def test_clock_times_and_dates_become_words(self):
        self.assertEqual(
            spoken("It is Monday, November 2, 2026, 3:15 PM at the store (America/Denver)."),
            "It is Monday, November second, twenty twenty six, three fifteen P M at the store "
            "(America/Denver).",
        )
        self.assertEqual(
            spoken("12:00 PM, 9:05 am, 10:30 AM"), "twelve P M, nine oh five A M, ten thirty A M"
        )
        self.assertEqual(spoken("Open until 9:05 a.m."), "Open until nine oh five A M.")
        self.assertEqual(spoken("January 1st, 2005"), "January first, two thousand five")
        self.assertEqual(spoken("on December 31"), "on December thirty first")
        self.assertEqual(spoken("May 3 1900"), "May third nineteen hundred")
        self.assertEqual(
            spoken("in 1999 and 2026"),
            "in one thousand nine hundred ninety nine and two thousand twenty six",
        )
        # Without AM or PM a time is left to the number rule, as before.
        self.assertEqual(spoken("3:15"), "three:fifteen")
        self.assertEqual(
            spoken("$129.00 at 5:15 PM"), "one hundred twenty nine dollars at five fifteen P M"
        )

    def test_number_words(self):
        self.assertEqual(number_words(0), "zero")
        self.assertEqual(number_words(15000), "fifteen thousand")
        self.assertEqual(number_words(249), "two hundred forty nine")

    def test_long_replies_stay_within_the_speech_limit(self):
        text = "It costs $129. " * 60
        normalized = spoken(text)
        chunks = speech_chunks(normalized)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1200 for chunk in chunks))
        self.assertEqual(" ".join(chunks), normalized.strip())
        self.assertEqual(normalized.count("one hundred twenty nine dollars"), 60)

    def test_clothing_sizes_are_spoken_as_words(self):
        self.assertEqual(spoken("Navy M. Sizes M and L."), "Navy medium. Sizes medium and large.")
        self.assertEqual(spoken("At 3:15 PM, size M."), "At three fifteen P M, size medium.")
