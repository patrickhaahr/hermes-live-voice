# Model context language

The voice speaks **English or Danish**, never another language.

The desktop sets `VOICE_LANG` from `navigator.language`: `da` when the locale
starts with `da`, otherwise `en`. It sends that value as `language` on
`/session`, `/codexlive/session`, and `/tool`, including voice previews. The
backend maps `da` to Danish and every other value, including a missing or
unrecognized one, to English. That value only chooses the call's default spoken
language: the language directive tells the model to answer Danish speech in
Danish and English speech in English, and to fall back to the default for any
other language or an unclear transcript.

Everything else addressed to a model is English: the chat prefix, spoken
acknowledgement instruction, delegation results and statuses, omitted-code
marker, preview instruction and sample, backend persona and chat instructions,
the voice delegation policy (its filler examples list English and Danish
phrases), the Codex skip instruction, and tool errors and timeouts. Tools are
always called with `language: "en"`; the voice model speaks their output in the
call's language. The desktop UI is English as well. The upstream Spanish strings
remain in `desktop/plugin.js` as the unused first argument of `tr()`, to keep the
fork close to upstream.

`language_directive.txt` ships both defaults. The exact shipped bundle, or either
default on its own, selects the built-in directive for the call's language.
Missing or blank files do the same. Any other file content is a custom override
and is included verbatim.

User requests, dynamic tool output, SOUL/persona source text, and memory are not
translated. A persona written in another language can still pull the model
toward it; the directive is a prompt, not a guarantee.

Tests use production function extraction and Node evaluation with transport and
host stubs, plus the broker boundary tests in `tests/test_codexlive_broker.py`.
They do not call providers or use a microphone.
