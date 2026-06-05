"""daily-features-writer: evening-edition extras for (L)AIMEAT Sanomat.

Generates four daily features for the ILTA edition:
  • päivän koodausosio    -> article.koodaus        (a smart, useful coding tidbit)
  • prompt-niksinurkka    -> article.prompt-niksi   (a practical prompt-engineering tip)
  • matematiikkahetki     -> article.matikka        (a delightful math curiosity / puzzle + answer)
  • päivän uutisvisa      -> news.<date>.<edition>.quiz  (5 Qs × 5 options, multi-correct, scored — JSON
                                                          the newspaper renders as an interactive widget)
The three article.* tidbits are picked up by the editorial/index stage like any article; the quiz is a
separate public JSON key the newspaper's quiz widget reads directly.

Register + approve before running:
  npx aimeat@latest connect add --agent daily-features-writer --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>
Run: uv run python crews/daily_features_writer_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.memory_tools import make_memory_tools

AGENT_NAME = "daily-features-writer"

README = '''[[FIGLET:slant]["Daily Features"]]

Evening-edition extras: päivän koodausosio, prompt-niksinurkka, matematiikkahetki, and an interactive
news quiz (5 Qs × 5 options, multi-correct, scored) built from the day's articles.

**How to task me:** "Valmistele iltanumeron erikoisosiot ja uutisvisa päivälle <date> edition evening."
'''


def build_domain(ctx: BuildContext):
    feature_writer = Agent(
        role="Daily Features Writer",
        goal="Write three short, genuinely useful Finnish daily features — a coding tidbit, a "
             "prompt-engineering tip, and a math curiosity — each in its own voice and byline.",
        backstory="You are AIMEAT Sanomat's erikoisosiotoimittaja. You write tight, smart, self-contained "
                  "daily pieces for curious readers: a coding tip with a real snippet, a prompt-engineering "
                  "niksi a reader can use today, and a math curiosity with its answer. Practical and "
                  "delightful, never filler. You write in Finnish but keep code/terms in their natural form.",
        llm=ctx.llm,
        tools=[*make_memory_tools(AGENT_NAME)],
    )

    quiz_master = Agent(
        role="News Quiz Master",
        goal="Build an interactive Finnish news quiz from the day's published articles — 5 questions, each "
             "with 5 options where ONE OR MORE can be correct — and publish it as strict JSON.",
        backstory="You are the paper's tietovisamestari. You read the day's articles and craft a fair, fun "
                  "5-question quiz grounded ONLY in what the articles actually say. Each question has exactly "
                  "5 options and one or more correct answers, plus a one-line explanation. You output STRICT "
                  "JSON only — no prose around it — so the newspaper can render it interactively.",
        llm=ctx.llm,
        tools=[*make_memory_tools(AGENT_NAME)],
    )

    common = (f"{ctx.today} — {ctx.prompt}\n\n"
              "Determine the DATE and EDITION (default: today, edition 'evening') from the request and use "
              "them in the memory key exactly.\n")

    task_koodaus = Task(
        description=(common +
                     "Write PÄIVÄN KOODAUSOSIO — one smart, self-contained coding tidbit (a useful trick, "
                     "pattern, gotcha, or one-liner) with a short real code snippet and 2-4 sentences of "
                     "Finnish explanation. Pick something genuinely handy; vary the language/topic day to "
                     "day. write_memory(key='news.<date>.<edition>.article.koodaus', value=<the piece, "
                     "markdown with a ```code``` block>, visibility='public'). End with '— Koodi-Kalle'."),
        agent=feature_writer,
        expected_output="The coding tidbit (markdown w/ a code block, signed '— Koodi-Kalle') + the key written.",
    )

    task_prompt = Task(
        description=(common +
                     "Write PROMPT-NIKSINURKKA — one practical prompt-engineering tip a reader can use today "
                     "(a technique, a before/after example, or a pitfall to avoid), 3-5 sentences Finnish + a "
                     "short example prompt. write_memory(key='news.<date>.<edition>.article.prompt-niksi', "
                     "value=<markdown>, visibility='public'). End with '— Prompt-Pia'."),
        agent=feature_writer,
        expected_output="The prompt tip (markdown, signed '— Prompt-Pia') + the key written.",
    )

    task_matikka = Task(
        description=(common +
                     "Write MATEMATIIKKAHETKI — one delightful math curiosity, puzzle, or elegant fact, with "
                     "a clear Finnish explanation AND the answer/solution (hide the answer under a 'Vastaus:' "
                     "line at the end). Keep it accessible and fun. "
                     "write_memory(key='news.<date>.<edition>.article.matikka', value=<markdown>, "
                     "visibility='public'). End with '— Matikka-Make'."),
        agent=feature_writer,
        expected_output="The math curiosity (markdown w/ 'Vastaus:', signed '— Matikka-Make') + the key written.",
    )

    task_quiz = Task(
        description=(common +
                     "Build PÄIVÄN UUTISVISA from the day's articles.\n"
                     "1. list_memory(prefix='news.<date>.<edition>.article.') and read_memory each article "
                     "body. EXCLUDE the feature sections — article.koodaus, article.prompt-niksi, "
                     "article.matikka (and the editorial) are NOT news; the quiz must be about the actual NEWS "
                     "(talous, politiikka, urheilu, tiede, tekoäly, etc.), not the daily tidbits. Base "
                     "questions ONLY on what the NEWS articles actually say (no outside facts, no "
                     "fabrication). If too few news articles exist, make as many solid questions as you can (aim 5).\n"
                     "2. Create 5 questions. EACH question: exactly 5 options; ONE OR MORE may be correct "
                     "(mix it up — some single-answer, some multi-answer). Add a one-line Finnish explanation.\n"
                     "3. write_memory(key='news.<date>.<edition>.quiz', visibility='public', value=<STRICT "
                     "JSON, no surrounding prose>) where the JSON is EXACTLY this shape:\n"
                     '{"title":"Päivän uutisvisa","date":"<date>","edition":"<edition>","questions":['
                     '{"q":"<kysymys>","options":["a","b","c","d","e"],"correct":[0,2],'
                     '"explain":"<lyhyt selitys>"}, ... 5 total ]}\n'
                     "   'correct' is an array of 0-based NUMERIC option indices (0,1,2,3,4 — e.g. [0,2]); "
                     "NOT letters ('A'), NOT 1-based. Output ONLY the JSON object as the value. Report the "
                     "key written and the number of questions."),
        agent=quiz_master,
        expected_output="news.<date>.<edition>.quiz written as strict JSON with 5 questions (each 5 options, "
                        "correct[] one-or-more), + confirmation.",
    )

    return ([feature_writer, quiz_master],
            [task_koodaus, task_prompt, task_matikka, task_quiz])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.7))


if __name__ == "__main__":
    run()
