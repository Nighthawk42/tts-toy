import os
import threading
import asyncio
import queue
import time
from typing import cast
from app_util import AppUtil
from constants import Constants
from l import L # type: ignore
from completions_config import CompletionsConfig
from completions_manager import CompletionsManager
from prefs import Prefs
from shared import Shared
from text_segmenter import TextSegmenter
from text_massager import TextMassager
from app_types import *
from ui import Ui
from util import Util
from constants_long import ConstantsLong
from audio_streamer import AudioStreamer

class App:
    """
    Rem, requires an LLM server (eg, llama-server or LM Studio) with the Orpheus model loaded. 
    And then update `config.json` with the appropriate url (eg, http://127.0.0.1:8080).
    """

    def __init__(self):

        AppUtil.init_logging()

        self.stop_audio_event = threading.Event()
        self.ui_queue = queue.Queue[UiMessage]()
        self.tts_queue = queue.Queue[TtsItem]()
        
        fatal_error_message, warning_message = Prefs().init(self.ui_queue)
        if fatal_error_message:
            print("\n" + fatal_error_message)
            exit(1)                  

        self.audio_streamer = AudioStreamer(
            stop_event=self.stop_audio_event, 
            tts_queue=self.tts_queue,
            ui_queue=self.ui_queue,
            completions_config=Prefs().orpheus_completions_config
        ) 

        with open(Constants.SYSTEM_PROMPT_FILE_PATH, 'r') as f:
            system_prompt = f.read() # don't catch exception
        if not system_prompt:
            raise Exception("System prompt is empty")

        self.llm_streamer_manager = CompletionsManager(
            config=cast(CompletionsConfig, Prefs().chat_completions_config), 
            system_prompt=system_prompt, 
            tts_queue=self.tts_queue,
            ui_queue=self.ui_queue
        )

        self.ui = Ui(self.on_enter)

        self.update_title()

        self.print_stroke_flag: bool = False # because needs to be declared first
        self.print_menu()
        self.print_stroke_flag: bool = True

        if warning_message:
            AppUtil.send_ui_message(self.ui_queue, LogUiMessage("[warning]" + warning_message))

        def go():
            AppUtil.ping_orpheus_server_with_feedback(Prefs().orpheus_completions_config, self.ui_queue)
            AppUtil.import_decoder_with_feedback(self.ui_queue)
        Util.run_in_thread(go, 0.5) # allows app to show UI before doing heavy load

    async def run(self):

        _ = asyncio.create_task(self.ui_message_queue_loop())

        try:
            await self.ui.application.run_async()
        except Exception as e:
            AppUtil.send_ui_message(
                self.ui_queue, 
                LogUiMessage(f"[error]Unexpected error. Could be bad. Consider restart.\n{e}") 
            )
        finally:
            pass

    async def process_user_input(self, user_input: str) -> None:

        user_input = user_input.strip()
        if not user_input:
            return
        
        # Process command, if necessary
        is_command = user_input.startswith("!") and user_input[1:].isalpha()
        if is_command:
            command = user_input[1:]
            await self.process_command(command)
            return
        
        if Prefs().ix_mode == "chat":
            await self.do_chat_request_plus(user_input)
        else:
            await self.play_direct_mode_text(user_input)

    async def process_command(self, command: str) -> None: 
        """
        Processes a "command" and prints feedback in content area. 
        """
        was_chat_mode = Prefs().ix_mode
        was_voice_code = Prefs().voice_code
        feedback = ""
        should_print_menu = False

        match command:

            case value if value in Constants.ORPHEUS_VOICE_CODES:
                Prefs().voice_code = command
                if Prefs().voice_code == "random":
                    feedback = "Changed voice to: Random voice per generated audio segment"
                else:
                    feedback = f"Changed voice to: {Prefs().voice_code}"
            
            case "clear":
                if Prefs().ix_mode == "chat":
                    self.llm_streamer_manager.init_history()
                    feedback = "Cleared chat history"
                    self.print_stroke_flag = True
                    await self.stop_all()
                else:
                    feedback = "Not in \"chat mode\n"

            case value if value in ["stop", "s"]:
                if False:
                    # TODO don't if no audio playing
                    feedback =  "Nothing to stop"
                else:
                    await self.stop_all()
                    feedback =  "Stopped audio "

            case value if value in ["direct", "d"]:
                if Prefs().ix_mode != "direct":
                    await self.stop_all()
                    Prefs().ix_mode = "direct"
                    feedback = "Switched to \"direct input mode\""
                    self.print_stroke_flag = True
                else:
                    feedback = "Already in \"direct input mode\""

            case value if value in ["chat", "c"]:
                if Prefs().ix_mode != "chat":
                    if not Prefs().chat_completions_config:
                        feedback = f"Can't. Chat mode is disabled (Edit \"{Constants.CONFIG_JSON_FILE_PATH}\")."
                    else:
                        Prefs().ix_mode = "chat"
                        feedback = f"Switched to \"chat mode\" ({Prefs().chat_completions_config.url})" # type: ignore
                        self.print_stroke_flag = True
                else:
                    feedback = "Already in chat mode"

            case "sync":
                Prefs().sync_text_to_audio = not Prefs().sync_text_to_audio
                feedback = "\"Sync text to audio playback\" set to: "
                feedback += "On" if Prefs().sync_text_to_audio else "Off"

            case "save":
                if Prefs().save_audio_to_disk:
                    Prefs().save_audio_to_disk = False
                    feedback = "\"Save audio output to disk\" set to: Off"
                else:
                    try:
                        os.makedirs(Prefs().audio_save_dir, exist_ok=True)
                        feedback = "\"Save audio output to disk\" set to: On"
                        feedback += f"\n[feedback_dark+i]{Prefs().audio_save_dir}"
                        Prefs().save_audio_to_disk = True
                    except Exception as e:
                        feedback = f"Problem with output directory {Prefs().audio_save_dir}: {e}"

            case value if value in ["help", "h", "menu"]:
                should_print_menu = True

            case value if value in ["q", "quit"]:
                self.ui.application.exit()

            case _:
                feedback = f"No such command: !{command}"

        will_print_something = (feedback or should_print_menu)
        if will_print_something:
            # Must stop all to prevent awkward async text. Can't be helped.
            await self.stop_all() 

        if feedback:
            self.print_to_content(f"[feedback+i]{feedback}")

        if should_print_menu:
            self.print_stroke_flag = True
            self.print_menu()
            self.print_stroke_flag = True

        title_dirty = Prefs().ix_mode != was_chat_mode or Prefs().voice_code != was_voice_code
        if title_dirty:
            self.update_title()

    async def do_chat_request_plus(self, user_input: str) -> None:
        """
        Starts an LLM streaming request, leading to audio output
        """
        if not Prefs().chat_completions_config:
            AppUtil.send_ui_message(self.ui_queue, 
                LogUiMessage(f"[error]Chat config missing! Edit \"{Constants.CONFIG_JSON_FILE_PATH}\" and fix."))
            return

        await self.stop_all()

        print_text = TextMassager.massage_user_input_for_print(user_input)
        self.print_to_content(print_text) 

        # Add the initial block for the assistant's response
        placeholder_text = f"[dark+i]Sending request..."
        self.print_to_content(placeholder_text)
        Shared.clear_placeholder_flag = True

        # Make the streaming request
        self.llm_streamer_manager.make_request(user_input, Prefs().voice_code, False)
        
    async def play_direct_mode_text(self, user_input: str) -> None: 
                
        user_input = TextMassager.transform_direct_mode_input_dev(user_input)
        
        if Prefs().sync_text_to_audio:
            placeholder_text = f"[dark+i]Starting..."
            self.print_to_content(placeholder_text)
            Shared.clear_placeholder_flag = True
        else:
            self.print_to_content(user_input) 

        await self.stop_all()

        time.sleep(0.1) # TODO revisit need for this

        segments = TextSegmenter.segment_full_message(user_input) 
        AppUtil.add_to_tts_queue(
            tts_queue=self.tts_queue,
            text_segments=segments, should_massage=False, voice_code=Prefs().voice_code, 
            has_message_start=True
        )
        AppUtil.add_to_tts_queue_end_item(tts_queue=self.tts_queue)

    # ---

    def print_to_content(self, message: str) -> None:        
        if Shared.clear_placeholder_flag:
            Shared.clear_placeholder_flag = False
            self.ui.content_control.model.erase_last_block()
        
        if self.print_stroke_flag:
            self.print_stroke_flag = False
            self.print_to_content(f"[dark][STROKE]")

        self.ui.content_control.model.add_block(message)
        self.ui.application.invalidate()

    def print_to_log(self, message: str) -> None:
        self.ui.log_control.model.add_block(message)
        self.ui.application.invalidate()

    def update_title(self) -> None:
        s = f"{Constants.APP_NAME} {Constants.VERSION} "
        s += "(chat mode)" if Prefs().ix_mode == "chat" else "(direct input mode)"
        s += f" (voice: {Prefs().voice_code})"
        self.ui.title_buffer.text = s

    def print_menu(self) -> None:
        s = ConstantsLong.MENU_TEXT
        s = ConstantsLong.MENU_TEXT.rstrip() + "\n\n"
        s = s.replace("%sync", f"(currently: {"on" if Prefs().sync_text_to_audio else "off"})")
        if Prefs().ix_mode == "chat":
            assert(Prefs().chat_completions_config)
            s += f"[feedback+i]You are in \"chat mode.\" The LLM will talk to you."
            s += f"\n[feedback_dark]({Prefs().chat_completions_config.url})" # type: ignore
        else:
            s += f"[feedback+i]You are in \"direct input mode.\"" 
            s += f"\n[feedback+i]Speech will be generated from your input."
        s = s.replace("%save", f"(currently: {"on" if Prefs().save_audio_to_disk else "off"})")
        self.print_to_content(s)

    def print_status(self, text: str) -> None:

        #self.ui.audio_status_buffer.reset()
        #self.ui.audio_status_buffer.insert_text(text)
        pass

    async def stop_all(self) -> None:
        """
        Stops most all the various machinery gracefully.
        """
        # Stop event will be cleared by the worker thread once acknowledged
        # TODO  may cause problems while in transitory "is-stopping-audio" state
        #       may need more logic (or just sleeping for half a second)
        self.stop_audio_event.set()
        
        self.llm_streamer_manager.abort()
        self.audio_streamer.clear_queues()
        AppUtil.clear_queue(self.ui_queue)
        Shared.synced_text_queue.clear()        

    async def ui_message_queue_loop(self):
        """
        Polls the ui message queue and updates the UI accordingly
        """
        while True:
            try:
                ui_message = self.ui_queue.get(block=False) 
                self.print_ui_message(ui_message)
                self.ui_queue.task_done()
            except queue.Empty:
                # This is the one queue handler which should be the fastest loop
                await asyncio.sleep(0.033)

    def print_ui_message(self, ui_message: UiMessage) -> None:
        """ 
        Updates a part of the UI based on ui_message's type 
        """
        if isinstance(ui_message, PrintUiMessage):
            self.print_to_content(ui_message.text)
        elif isinstance(ui_message, StreamedPrintUiMessage):
            if not Prefs().sync_text_to_audio: 
                if Shared.clear_placeholder_flag:
                    self.ui.content_control.model.replace_last_block(ui_message.text)
                else:
                    self.ui.content_control.model.append_to_last_block(ui_message.text)
                Shared.clear_placeholder_flag = False            
        elif isinstance(ui_message, SyncedPrintUiMessage):
            if Prefs().sync_text_to_audio:
                if Shared.clear_placeholder_flag:
                    self.ui.content_control.model.replace_last_block(ui_message.item.display_text)
                else:
                    self.ui.content_control.model.append_to_last_block(ui_message.item.display_text)
                Shared.clear_placeholder_flag = False
        elif isinstance(ui_message, LogUiMessage):
            self.print_to_log(ui_message.text)
        elif isinstance(ui_message, GenStatusUiMessage):
            self.ui.update_gen_status(ui_message.item)
        elif isinstance(ui_message, AudioBufferUiMessage):
            self.ui.update_audio_status(ui_message.seconds)
        
    async def on_enter(self) -> None:
        user_input = self.ui.input_buffer.text
        self.ui.input_buffer.reset()
        await self.process_user_input(user_input)

# ---

if __name__ == "__main__":
    app = App()
    asyncio.run(app.run())
