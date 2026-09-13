import unittest

from neuro_voice.memory.conversation import ConversationManager


class AutonomousConversationBridgeTests(unittest.TestCase):
    def test_internal_instruction_is_transient_but_spoken_reply_is_continuous(self):
        conversation = ConversationManager("persona")
        conversation.add_user("会話って、お互いに考えを出すものだよね")
        instruction = "[internal autonomous action] 今の文脈から一つ問いかける"

        generation_messages = conversation.messages_for_autonomous_turn(instruction)

        self.assertEqual("会話って、お互いに考えを出すものだよね",
                         generation_messages[-2]["content"])
        self.assertEqual(instruction, generation_messages[-1]["content"])
        self.assertNotIn(instruction, [item["content"] for item in conversation.messages()])

        conversation.add_assistant("私は、意見が少し違う瞬間も対話だと思う。きみは？")
        conversation.add_user("うん、そこは私も大事だと思う")
        durable = conversation.messages()

        self.assertEqual("assistant", durable[-2]["role"])
        self.assertIn("意見が少し違う", durable[-2]["content"])
        self.assertEqual("user", durable[-1]["role"])
        self.assertNotIn(instruction, [item["content"] for item in durable])


if __name__ == "__main__":
    unittest.main()
