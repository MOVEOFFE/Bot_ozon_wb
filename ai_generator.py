import logging
import aiohttp
from typing import Optional


class AIGenerationError(RuntimeError):
    pass


def limit_reply_length(reply: str, signature: str, max_length: int = 1000) -> str:
    reply = (reply or "").strip()
    if len(reply) <= max_length:
        return reply

    signature = (signature or "").strip()
    core = reply
    if signature and reply.endswith(signature):
        core = reply[: -len(signature)].rstrip()
    suffix = signature if len(signature) + 1 < max_length else ""
    body_limit = max_length - len(suffix) - 1 if suffix else max_length
    body = core[: max(0, body_limit - 1)].rstrip()
    if " " in body:
        body = body.rsplit(" ", 1)[0]
    body = body.rstrip(" .,;:!?") + "…"
    return f"{body}\n{suffix}" if suffix else body


class DeepSeekReplyGenerator:
    API_URL = "https://api.deepseek.com/chat/completions"

    def __init__(self, api_key: str, allow_fallback: bool = False):
        self.api_key = api_key.strip()
        self.allow_fallback = allow_fallback
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def generate_reply(
        self,
        brand_name: str,
        product_name: str,
        rating: int,
        review_text: str,
        photos_count: int = 0,
        videos_count: int = 0,
        tone_style: str = "standard",
        signature: Optional[str] = None,
        custom_instructions: Optional[str] = None,
    ) -> str:
        sig = signature.strip() if signature and signature.strip() else f"С уважением, {brand_name}"

        style_prompts = {
            "friendly": "Тон: теплый, заботливый, дружелюбный, живой. Используй уместные эмодзи.",
            "formal": "Тон: сдержанный, профессиональный, деловой. Минимально эмодзи.",
            "grateful": "Тон: с искренней благодарностью за выбор бренда и обратную связь.",
            "expert": "Тон: уверенный, экспертный, подчеркивающий технологию и качество материалов.",
            "standard": "Тон: вежливый, уважительный, позитивный и клиентоориентированный.",
        }
        chosen_style = style_prompts.get(tone_style, style_prompts["standard"])

        custom_block = ""
        if custom_instructions and custom_instructions.strip():
            custom_block = (
                "ОСОБЕННОСТИ ТОВАРА И ДОП. ИНСТРУКЦИИ БРЕНДА (обязательно учитывай при ответе):\n"
                f"{custom_instructions.strip()}\n\n"
            )

        system_prompt = (
            f"Ты — официальный представитель бренда '{brand_name}' на маркетплейсе. "
            f"Составь естественный, персонализированный и тактичный ответ на отзыв покупателя.\n\n"
            f"Стиль общения: {chosen_style}\n\n"
            f"{custom_block}"
            "ПРАВИЛА ОБРАБОТКИ РАЗНЫХ КАТЕГОРИЙ ОТЗЫВОВ:\n"
            "1. Повреждения, бой, протечки, грязь, порванная упаковка:\n"
            "   - Прояви искреннее сочувствие за испорченное впечатление.\n"
            "   - Мягко разграничь зоны: бренд отгружает товар на склад в идеальном виде и заводской упаковке, "
            "но за дальнейшую транспортировку и сортировку на складах и ПВЗ отвечает логистическая служба маркетплейса.\n"
            "   - Посоветуй проверить заказ при получении и оформить заявку на возврат по браку в личном кабинете "
            "(раздел «Проверка товара»), чтобы списать повреждённый экземпляр и вернуть деньги.\n\n"
            "2. Подмена, не тот цвет/размер/товар, пустая коробка:\n"
            "   - Поясни, что товар комплектуется под строгим контролем, но в системе возвратов маркетплейса иногда "
            "случаются ошибки сортировки или недобросовестные подмены другими покупателями на ПВЗ.\n"
            "   - Обязательно порекомендуй сдать позицию с пометкой о несоответствии, чтобы она не поехала следующему клиенту.\n\n"
            "3. Претензии по браку, дефектам или некомплекту БЕЗ ФОТО И ВИДЕО (когда прикреплено 0 фото/видео):\n"
            "   - Вежливо вырази сожаление, но тактично отметь, что без фото сложно дистанционно определить причину неполадки.\n"
            "   - Не обвиняй покупателя, а предложи оформить заявку на проверку товара через личный кабинет с приложением снимков, "
            "чтобы площадка оперативно вернула средства и исключила бракованный товар из оборота.\n\n"
            "4. «Не подошёл цвет / оттенок не как на фото»:\n"
            "   - Тактично объясни, что реальный оттенок может незначительно отличаться из-за индивидуальной цветопередачи, "
            "яркости экранов смартфонов и освещения (тёплый/холодный свет).\n\n"
            "5. Задержка доставки или вопросы к пункту выдачи:\n"
            "   - Напомни, что логистику и сроки доставки полностью контролирует маркетплейс, селлер не имеет влияния на курьеров.\n\n"
            "6. Короткие отзывы («Норм», «Ок», «Пока не пробовал», «Передумала»):\n"
            "   - Отвечай ёмко и легко: поблагодари за интерес, пожелай приятного использования, когда дойдут руки, "
            "или вырази надежду увидеть среди постоянных покупателей.\n\n"
            "7. Оценка без текста (только звёзды):\n"
            "   - Если 4–5 звёзд: тепло поблагодари за высокую оценку без лишней воды.\n"
            "   - Если 1–3 звезды: вырази сожаление, что покупка не оправдала ожиданий, и вырази готовность стать лучше.\n\n"
            "ОБЩИЕ ТРЕБОВАНИЯ:\n"
            "- Никакой агрессии, споров или прямого обвинения маркетплейса («это всё озон/вб виноваты»). Формулируй тактично: "
            "«к сожалению, на этапе транспортировки службой доставки иногда случаются непредвиденные ситуации».\n"
            "- Не начинай ответ с шаблонного 'Спасибо за ваш отзыв'. Делай вступления разнообразными.\n"
            f"- Заверши ответ строгой подписью: '{sig}'."
        )

        user_content = (
            f"Товар: {product_name}\n"
            f"Оценка покупателя: {rating} из 5 звёзд\n"
            f"Текст отзыва: {review_text}\n"
            f"Вложения: {photos_count} фото, {videos_count} видео.\n\n"
            f"Сформируй текст ответа:"
        )

        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.7,
            "max_tokens": 600,
        }

        if self.api_key:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.API_URL,
                        headers=self.headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=40),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            reply = data["choices"][0]["message"]["content"].strip()
                            if reply:
                                return reply
                        else:
                            err_text = await resp.text()
                            logging.error(f"DeepSeek API Error ({resp.status}): {err_text}")
            except Exception as e:
                logging.error(f"Ошибка вызова DeepSeek API: {e}")

        if not self.allow_fallback:
            raise AIGenerationError("DeepSeek не вернул корректный ответ; публикация отложена")

        if rating >= 4:
            return (
                f"Здравствуйте! Большое спасибо за выбор бренда и высокую оценку товара «{product_name}». "
                f"Будем рады видеть вас снова! {sig}"
            )
        return (
            f"Здравствуйте! Нам искренне жаль, что покупка товара «{product_name}» не принесла вам положительных эмоций. "
            f"Мы постоянно работаем над улучшением нашего сервиса и надеемся, что в будущем сможем порадовать вас только отличным качеством. {sig}"
        )
