import unittest

from ai_generator import DeepSeekReplyGenerator, limit_reply_length


class AIGeneratorTests(unittest.TestCase):
    def test_sanitize_output_strips_markdown_and_contacts(self):
        g = DeepSeekReplyGenerator
        self.assertEqual(g._sanitize_output("**жирный** текст"), "жирный текст")
        self.assertEqual(g._sanitize_output("Ответ: Привет"), "Привет")
        self.assertNotIn("example.com", g._sanitize_output("Смотри https://example.com/page тут"))
        self.assertNotIn("test@mail.ru", g._sanitize_output("Почта test@mail.ru здесь"))
        self.assertNotIn("@username", g._sanitize_output("Мой @username тут"))
        self.assertNotIn("999 123-45-67", g._sanitize_output("Звони +7 999 123-45-67 пожалуйста"))
        self.assertIn("291912901", g._sanitize_output("Артикул 291912901"))

    def test_ensure_signature_no_duplicate(self):
        g = DeepSeekReplyGenerator
        sig = "Команда магазина"
        out = g._ensure_signature("Отличный товар!\n\nС уважением,\nКоманда магазина", sig)
        self.assertEqual(out.count("Команда магазина"), 1)
        out2 = g._ensure_signature("Отличный товар!", sig)
        self.assertEqual(out2.count("Команда магазина"), 1)
        self.assertTrue(out2.endswith("Команда магазина"))

    def test_limit_reply_length_cuts_by_sentence_and_keeps_signature(self):
        sig = "С уважением, Команда"
        long_text = "Первое предложение про товар. Второе предложение про качество. " * 20
        out = limit_reply_length(long_text, sig, max_length=120)
        self.assertLessEqual(len(out), 120)
        self.assertTrue(out.endswith(sig))


if __name__ == "__main__":
    unittest.main()
