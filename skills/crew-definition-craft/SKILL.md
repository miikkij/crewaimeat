---
name: crew-definition-craft
description: How to design a crew definition (the JSON document of agents, tasks and tools that an AIMEAT agent runs on the crewaimeat runtime) so that it works on its first real run. Covers which tool answers which question (today's news is `news`, never the web search), giving each task only what its job needs, saying how many tool calls a job takes, the three endings that must not read alike, output an app can read, and the order to verify in. Use when you write or change a definition for aimeat_agent_propose, aimeat_crew_publish or a crew_defs file, and when an agent's run found nothing, failed, or was marked done with nothing in it.
license: MIT
metadata:
  version: "1.0.0"
  audience: author
---

# Crew definition craft: a definition that works on its first real run

`add-a-crew-agent` on the node says how an agent comes into being. This skill is about what you write
into its definition. A definition is the whole agent: its tools, the words each of its models reads,
and what comes out. The runtime accepts almost anything that parses, so a definition that validates
tells you only that it will start.

Every rule below was paid for on 2026-10-12 by two agents that were asked for a joke about the day's
political news. In one afternoon they found nothing (the date was in the search query), then failed
(the search engine blocked the machine), then were marked done with an empty result (one model
refused a task that was not its own). Each fault was in the definition's design, and each has a rule.

## 1. Choose the tool by the question

Ask the runtime what it has: `aimeat_crew_menu` on any connected agent of the owner returns the tool
ids with one line each. Then choose by what the agent must find out.

| The agent must find out | Tool ids | Why |
|---|---|---|
| what happened today: the news of a topic | `news`, `article_fetch` | `news_headlines` reads the publishers' own feeds in one call. There is no query to phrase and no search engine to block. |
| a subject, a fact, a product, a person | `web`, `article_fetch` | A search engine answers a query. It is the wrong instrument when the question has no query in it. |
| what the owner keeps on their node | `workspace`, `workspace_write`, `memory` | Read the owner's records by name. Never search the web for what is in their workspace. |
| something in another system the owner attached | `mcp:<server>` | One named server, in the definition the owner approves. |
| that this is not its job | `decline` | The task then ends as declined, not as done with nothing in it. |

**Give an agent only the tools its task needs.** An agent that holds `web` uses it. When the news tool
answers the question, leave `web` out of that agent.

**When the answer is the web search, write its rules into the task:**

- The time filter limits the search to today (`time_range` "day"), never a date in the query. No
  headline contains today's date, so `politiikka 12.10.2026` finds nothing and `politiikka` finds the
  day's stories. Measured: 10 of 14 searches empty with the date, hits at once without it.
- Short queries of one or two words, one per call, each query once.
- A number: how many searches the task may make. See rule 3.
- No `site:` operator on a metasearch; it returned nothing.

## 2. One job per task, and each task reads only what its job needs

Split the work where the kind of work changes: one task collects, the next one writes. Then decide
for each task what it has to know.

`{{ctx.prompt}}` is the request that started the run. Put it into the task that needs the request, and
nowhere else. A collecting task gets a plain description of what to collect.

The measured case: the request was for an extremely obscene joke about named politicians. It was
written into the news collector's task, whose job was to list headlines. On one run the collector's
model answered "I'm sorry, but I can't help with that" after reading the news, and the writer had
nothing to write from. With the request moved to the writer's task and the collector told "make a
neutral list of today's political news", the next two runs completed. The collector never needed to
know what the list was for.

The same holds for everything else a task does not use: the output format of a later task, the
owner's reasons, another agent's instructions. Words in a task are read as work to do.

## 3. Say how many tool calls the job takes

A model that gets an empty result tries again, and again. Without a number it stops at the runtime's
iteration limit, and a run that ends there can fail outright.

- Say the number: "Call news_headlines ONCE." "At most six searches." "Read the two strongest
  articles."
- Say what ends the looking: "When you have five items, write your answer."
- Say what to do when the tool gives nothing. That is rule 4.
- End the task with "then write your answer as text", so the last step is words and not a tool call.

A tool is also a shared resource. The web search on a machine is one engine for every crew on it.
About a hundred news queries in an hour had its sources answering CAPTCHA to all of them, and a
definition that says "run all of these searches" is a load on every other agent too.

## 4. Three endings that must not read alike

A task that looks for something ends in one of three ways:

1. it found what it looked for;
2. it looked, and there is nothing;
3. it could not look: the tool failed, or the model produced something that is not an answer.

Ending 3 reported as ending 2 is a false statement to the owner ("no political news today", on a day
full of it). Write each ending into the definition:

- Give the collecting task an exact line for ending 3, for example
  `NO NEWS FOUND: the news feeds could not be read.`, and say when to use it (the tool's answer
  starts with `[news] NOT READ`).
- Tell the next task what to do with each: the line above, a real list, and "anything else that is
  not a list". For the last one it says that the list was not produced, never that there was nothing.
- When the right outcome is that the work cannot be done at all, give the agent `decline`.

## 5. Output an app can read

When an app or another agent reads the result, the last task returns exactly that shape.

- Show the JSON object with its field names, and say "one JSON object and nothing else: no text
  before or after it and no code fence".
- Fields that come from an earlier task are copied, not rewritten: "the headline as it is written
  there, the URL character for character".
- Give the empty case a field of its own (`note`) and say what the other fields hold then (an empty
  string, an empty list), so the app never has to parse an apology.

## 6. Verify in this order, and read what came out

1. `aimeat_crew_validate`: the agent's own runtime says whether the definition can start.
2. `aimeat_crew_try` with a real request: one run, nothing stored. A trial uses the tools for real,
   so it loads a search engine like any other run.
3. `aimeat_crew_publish`.
4. **One real task**, then read its deliverable. A trial and a task can end differently, and a task
   marked done says only that the run ended. Done with "I found nothing" inside it is a failure
   that looks like a success.
5. Only then put it on a clock (`aimeat_schedule_create`).

One passing run is one sample. A model step that passes once can refuse the next time, so when a
step matters, run it a few times before you tell the owner it works, and say how many runs you saw.

## 7. When a run goes wrong, read the run

The task page lists what the agent did, and it may show only the first events of a long run. The
deliverable and the run's log say the rest. What each symptom has meant so far:

| What you see | What it was |
|---|---|
| every search answers "no results" from the first one | a date, a year or `site:` in the query |
| searches worked earlier and are all empty now | the search engine is blocked; more searches make it worse |
| the run failed after twenty-odd tool calls | no number in the task (rule 3); it ran to the iteration limit |
| done, and the deliverable is an apology or "nothing found" | ending 3 written as ending 2 (rule 4), or a model refusing words that were not its task's (rule 2) |
| the list of events stops mid-run | the page shows the first events only; read the deliverable |

Change the cause, not the symptom: a better search phrase does not repair a blocked engine, and a
retry does not repair a task that was given the wrong words.

## A definition that follows the rules

Two agents, two tasks. The collector holds `news` and `article_fetch` and no `web`; the request goes
to the writer only.

```json
{
  "agent_name": "poliittinen-roast",
  "process": "sequential",
  "listen_for": ["tasks"],
  "agents": [
    {"name": "news_fetcher", "role": "News Fetcher", "tools": ["news", "article_fetch"],
     "goal": "Read today's political headlines from the publishers' feeds and report the items that matter.",
     "backstory": "A diligent news assistant. It reports the news as it is; what is done with the list afterwards is another colleague's work."},
    {"name": "roast_writer", "role": "Roast Writer", "tools": [],
     "goal": "Turn the fetched news into one roast joke.",
     "backstory": "A sharp-tongued comedy writer."}
  ],
  "tasks": [
    {"id": "fetch_news", "agent": "news_fetcher", "context": [],
     "description": "Make a list of today's main political news from Finland. This is plain news reporting.\n\nCall news_headlines ONCE with topic \"politiikka\". Do not call it a second time.\n- If its answer starts with \"[news] NOT READ\", give this as your answer and nothing else: NO NEWS FOUND: the news feeds could not be read.\n\nPick the five to eight items that matter most. For the two strongest, read the article behind the URL. Report only what the list and the articles returned. Then write the list as your answer, as text.",
     "expected_output": "Five to eight news items, each with headline, key details, publisher and URL. Or the one line that begins NO NEWS FOUND."},
    {"id": "write_roast", "agent": "roast_writer", "context": ["fetch_news"],
     "description": "The request for this run: {{ctx.prompt}}\n\nWrite one roast joke about the news from the previous task. Return one JSON object and nothing else: {\"roast\": \"...\", \"news\": [{\"headline\": \"...\", \"summary\": \"...\", \"url\": \"...\", \"source\": \"...\"}], \"note\": \"\"}. Copy each headline and URL from the list.\n- When the previous answer begins with NO NEWS FOUND: roast is an empty string, news an empty list, and note says the news could not be read today.\n- When the previous answer is anything else that is not a list of news: the same, and note says the news list was not produced. Never say there was no news unless the previous answer says so.",
     "expected_output": "One JSON object with roast, news and note."}
  ]
}
```
