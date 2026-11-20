import unittest
from unittest.mock import Mock, patch

from app.config import GroupInitiative, Settings, group_initiative_overrides
from app.group import GroupReplies, ReplyKind


class GroupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.settings = Mock(spec=Settings)
        self.settings.initiative_for.return_value = GroupInitiative(chance_percent=100)
        self.policy = GroupReplies(self.settings)
        clock = patch('app.group.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def push(self, message_id, text='Сообщение', *, chat=-1, user=10, direct=False,
             fresh=True, may_initiate=True):
        return self.policy.consider(
            chat_id=chat, user_id=user, message_id=message_id, text=text,
            direct=direct, fresh=fresh, may_initiate=may_initiate,
        )

    def test_requires_thirty_messages_and_900_seconds_after_previous_reply(self):
        for mid in range(1, 29):
            self.assertIsNone(self.push(mid))
        self.assertIsNone(self.push(29, 'На Linux снова проблема с ядром'))
        self.assertEqual(self.push(30, 'Обнови драйвер и пакет', user=20), ReplyKind.INITIATIVE)
        self.now += 25
        self.policy.finish(-1)
        self.now += 899

        for mid in range(31, 59):
            self.assertIsNone(self.push(mid))
        self.assertIsNone(self.push(59, 'В Arch обновили ядро'))
        self.assertIsNone(self.push(60, 'Драйвер теперь стабильнее', user=20))
        self.now += 1
        self.assertEqual(self.push(61, 'У меня на Linux всё работает'), ReplyKind.INITIATIVE)
        self.policy.finish(-1)

    def test_single_word_single_author_and_unrelated_or_stale_context_do_not_trigger(self):
        for mid in range(1, 31):
            self.assertIsNone(self.push(mid, 'Steam', user=10 + mid % 2))
        self.assertIsNone(self.push(31, 'Steam и квест', user=10, may_initiate=False))
        self.assertIsNone(self.push(32, 'Установила ядро в Arch', user=10))
        self.assertIsNone(self.push(33, 'Драйвер работает', user=10))
        self.assertIsNone(self.push(34, 'Смотрю аниме', user=20))
        self.now += 181
        self.assertIsNone(self.push(35, 'В Linux можно обновить драйвер', user=20))

    def test_topics_and_thresholds_are_separate_by_chat(self):
        for mid in range(1, 30):
            self.push(mid, chat=-1)
        self.push(29, 'Драйвер в Linux', chat=-2, user=20)
        self.assertIsNone(self.push(30, 'Обновили ядро в Arch', chat=-1))
        self.assertIsNone(self.push(30, 'Обновили ядро в Arch', chat=-2))
        self.assertEqual(self.push(31, 'Драйвер в Linux', chat=-1, user=20), ReplyKind.INITIATIVE)
        self.policy.finish(-1)
        self.assertIsNone(self.push(31, 'Драйвер в Linux', chat=-2, user=20))

    def test_150_participants_cannot_queue_multiple_replies_in_one_chat(self):
        self.assertEqual(self.push(1, direct=True), ReplyKind.DIRECT)
        for user in range(11, 160):
            self.assertIsNone(self.push(user, direct=True, user=user))
        self.policy.finish(-1)
        self.assertIsNone(self.push(161, direct=True, user=11))
        self.now += 8
        self.assertIsNone(self.push(162, direct=True, user=10))
        self.assertEqual(self.push(163, direct=True, user=11), ReplyKind.DIRECT)
        self.policy.finish(-1)

    def test_global_group_capacity_and_one_initiative_at_a_time(self):
        for chat in (-1, -2, -3):
            self.assertEqual(self.push(1, chat=chat, direct=True), ReplyKind.DIRECT)
        self.assertIsNone(self.push(1, chat=-4, direct=True))
        self.policy.finish(-3)
        self.assertEqual(self.push(2, chat=-4, direct=True), ReplyKind.DIRECT)
        for mid in range(1, 30):
            self.push(mid, chat=-5)
        self.push(30, 'Видеокарта Radeon', chat=-5)
        self.assertIsNone(self.push(31, 'Новый кулер', chat=-5, user=20))
        for chat in (-1, -2, -4):
            self.policy.finish(chat)
        self.assertEqual(self.push(32, 'На Radeon нужен кулер', chat=-5), ReplyKind.INITIATIVE)
        self.policy.finish(-5)

    def test_disabled_group_and_old_updates_still_allow_fresh_direct_reply(self):
        self.settings.initiative_for.side_effect = lambda chat: GroupInitiative(
            enabled=chat != -1, min_messages=5, chance_percent=100,
        )
        for mid in range(1, 5):
            self.push(mid, chat=-1)
        self.push(5, 'Смотрю аниме', chat=-1)
        self.assertIsNone(self.push(6, 'У манги хорошая концовка', chat=-1, user=20))
        self.assertIsNone(self.push(7, direct=True, fresh=False))
        self.assertEqual(self.push(8, direct=True), ReplyKind.DIRECT)
        self.policy.finish(-1)

    def test_abuse_mute_is_per_user_per_chat_and_expires(self):
        self.assertEqual(self.push(1, 'Аяка, заткнись', direct=True), ReplyKind.BOUNDARY)
        self.policy.finish(-1)
        self.now += 1
        self.assertIsNone(self.push(2, 'Аяка, ты тупая', direct=True))
        self.now += 30
        self.assertIsNone(self.push(3, 'Аяка, помоги', direct=True))
        self.assertEqual(self.push(4, 'Аяка, привет', chat=-2, direct=True), ReplyKind.DIRECT)
        self.policy.finish(-2)
        self.assertEqual(self.push(5, 'Аяка, это тупая ошибка', user=20, direct=True), ReplyKind.DIRECT)
        self.policy.finish(-1)
        self.now += 570
        self.assertEqual(self.push(6, 'Аяка, привет', direct=True), ReplyKind.DIRECT)
        self.policy.finish(-1)

    def test_forget_removes_participant_from_recent_topic(self):
        for mid in range(1, 29):
            self.push(mid)
        self.push(29, 'Linux и Arch')
        self.policy.forget(-1, 10)
        self.assertIsNone(self.push(30, 'Драйвер ядра', user=20))

    def test_roll_rate_is_bounded_even_if_topic_matches_repeatedly(self):
        self.settings.initiative_for.return_value = GroupInitiative(min_messages=5)
        for mid in range(1, 5):
            self.push(mid)
        with patch('app.group.random.randrange', return_value=99) as draw:
            self.push(5, 'Linux и Arch')
            self.push(6, 'Обновили ядро', user=20)
            self.push(7, 'Драйвер Linux', user=10)
            self.assertEqual(draw.call_count, 1)
            self.now += 15
            self.push(8, 'Ядро в Arch', user=20)
            self.assertEqual(draw.call_count, 2)

    def test_inflections_of_one_keyword_do_not_make_a_topic(self):
        self.settings.initiative_for.return_value = GroupInitiative(min_messages=5, chance_percent=100)
        for mid in range(1, 5):
            self.push(mid)
        self.assertIsNone(self.push(5, 'Обсуждали мангу'))
        self.assertIsNone(self.push(6, 'Какая манга?', user=20))
        self.assertEqual(self.push(7, 'Аниме по этой манге', user=10), ReplyKind.INITIATIVE)
        self.policy.finish(-1)

    def test_supported_interests_trigger_only_after_related_replies(self):
        self.settings.initiative_for.return_value = GroupInitiative(min_messages=5, chance_percent=100)
        for chat, (opening, reply) in enumerate((
            ('Купил игру', 'В Steam часто скидки на игры'),
            ('Начала смотреть аниме', 'Озвучка хорошая'),
            ('Дочитала мангу', 'Я дочитал манхву'),
            ('Поставил Linux', 'Драйвер уже работает?'),
            ('Купил видеокарту', 'У меня был Radeon'),
        ), start=1):
            with self.subTest(opening=opening):
                for mid in range(1, 5):
                    self.push(mid, chat=-chat)
                self.assertIsNone(self.push(5, opening, chat=-chat))
                self.assertEqual(self.push(6, reply, chat=-chat, user=20), ReplyKind.INITIATIVE)
                self.policy.finish(-chat)


class GroupConfigurationTests(unittest.TestCase):
    def test_partial_overrides_and_independent_ids(self):
        settings = group_initiative_overrides(
            '{"-1001":{"enabled":false},"-1002":{"min_messages":50,"chance_percent":25}}',
            GroupInitiative(cooldown_seconds=1200),
        )
        self.assertEqual(settings[-1001], GroupInitiative(enabled=False, cooldown_seconds=1200))
        self.assertEqual(settings[-1002], GroupInitiative(cooldown_seconds=1200,
                                                        min_messages=50, chance_percent=25))

    def test_invalid_configuration_is_rejected(self):
        for raw in ('[]', '{"1001":{}}', '{"-1001":{"enabled":"false"}}',
                    '{"-1001":{"min_messages":true}}', '{"-1001":{"cooldown_seconds":1}}',
                    '{"-1001":{"typo":false}}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'GROUP_INITIATIVE_JSON'):
                group_initiative_overrides(raw, GroupInitiative())
