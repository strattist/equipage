# Nutritionist agent: design

Status: implemented and merged (PR #2); live verification against a real Discord and Mealie (plan Task 9) is still to do. See the plan's implementation record.

## Intent and why

Add a second Discord agent, the nutritionist, next to the coach. The user wants to lose weight progressively and durably. Training is handled by the coach; this agent makes eating match it with minimal effort: it plans the week's meals in the user's Mealie instance, produces the shopping list, and works from what is already in the fridge. The user cooks every evening and eats the leftovers the next lunch, so the plan is built around that rhythm.

## Goals

- Own Discord channel `#nutrition`, French, same bot process as the coach.
- Weekly meal plan written to Mealie (recipes found or created via the Mealie API), plus a shopping list. **Nothing is written to Mealie until the user confirms** (see Confirmation).
- The user decides how many new recipes a week contains (see Flow), even when existing Mealie recipes would fit.
- Every recipe is for 2 portions. The user scales the quantities if more people eat.
- The user can tell it what is in the fridge, and the plan and shopping list use it.
- The agent asks which days the user is away; those days are skipped.
- Dinners alternate protein / veggie, with protein dinners on exercise days (see Rules).
- Linked to the coach through the shared SQLite database: exercise days come from the coach's planned sessions.

## Non-goals (v1)

- No automatic re-plan when the coach changes the week; the user asks "replan" (or the weekly run picks it up).
- No calorie or macro tracking, no meal logging, no photo input.
- No medical or dietary advice beyond the principles below; no hard rules were given.
- No editing or deleting of existing Mealie recipes.

## Anti-goals

- The agent writes anything to Mealie (recipes, meal plan entries, shopping list items) without an explicit confirmation from the user.
- The agent keeps a plan only in the chat. Once confirmed, Mealie is the source of truth for the plan.
- The agent changes or deletes the user's Mealie data other than the meal plan entries and shopping list items it created.
- Surprising the user with a long questionnaire. At most one weekly question round (away days, fridge).

## Constraints

- Mealie: REST API with the user's API token (created at `/user/profile/api-tokens`), docs served by the user's instance at `/docs`. Exact endpoint paths and the auth header must be taken from that instance's `/docs`; they were not confirmed from public docs. The bot container must reach Mealie on the NAS. Secrets: `MEALIE_URL`, `MEALIE_API_TOKEN` in `.env`.
- Same stack as the coach: Python 3.12, `anthropic` tool runner, discord.py, APScheduler, SQLite under `/data`.
- Language policy: see the coach spec ("Language policy"). In short, the nutritionist speaks French to the user and writes all Mealie content in French (recipe names, ingredients, steps, tags `protéines` / `végétarien`, shopping items); the database, code, prompts (English, telling the model to reply in French), README and commands are English. Training kinds shown in the dinner table and dinner types are displayed with French labels by the code (rowing → rameur, protein → protéines, etc.).
- Runs on TrueNAS in the same container.

## Approaches considered

- **A. Plain Python tools calling Mealie's REST API (chosen by the user).** Same pattern as the coach, easy to learn, narrow permissions, about 100 lines of wrapper.
- B. A community Mealie MCP server. Less code but a third-party dependency that was not vetted, harder to limit what the agent can do, and less to learn. Rejected.
- C. One combined coach+nutritionist agent. Contradicts the goal of two agents with their own channels. Rejected.

## Rules (decided by the user)

- The user cooks every evening and has enough for the next lunch.
- Dinners alternate veggie and protein; protein dinners fall on exercise days. The user confirmed it makes no real nutritional difference whether the protein meal is lunch or dinner, so the protein meal is the dinner of an exercise day, and its leftovers are the next day's lunch.
- The user can give the fridge contents.
- The agent asks about away days; the user can skip those days.

## Decisions made by the design (my calls, for the user to check)

1. **Rules live in code, not in the prompt.** A tool `get_meal_targets(start, end)` returns, per day: dinner type (`protein` or `veggie`), lunch source (the previous evening's dinner), `away` flag, and portions to cook. The model chooses recipes; it does not decide the rules.
2. **Exercise day** = a planned session of kind `rowing`, `hiit` or `strength` in the coach's `planned_sessions`. `walk`, `mobility`, `rest` and days with no plan count as non-exercise. If the coach has not planned a date, fall back to the weekly structure in `profile.json`.
3. **Away day = whole day skipped** (lunch and dinner). Consequences: no dinner cooked that day, so the next day's lunch has no leftover and is left empty for the user to decide. The dinner cooked the evening before still yields 2 portions, so its second portion has no lunch to feed; the table flags it ("spare portion: freeze or eat") and the user decides.
4. **Portions (decided by the user)**: every recipe is for 2 portions (dinner + next lunch), always. No scaling logic in the agent.
5. **Weekly run**: Sundays at 10:00 (`NUTRITION_WEEKLY_HOUR`), the agent posts the question round: away days next week, fridge changes, and **how many new recipes the user wants this week**. The user can also state it any time in the chat ("3 new recipes this week"). The agent plans after the user replies. On demand any time.
5b. **New recipes**: the requested count of dinners get brand-new recipes (written by the agent, with ingredients and steps, as drafts). The rest reuse existing Mealie recipes of the right type. A new recipe is proposed even if an existing one fits. If Mealie has no fitting recipe for a slot, the agent says so and proposes a new one for it.
5c. **Confirmation (decided by the user)**: the agent proposes, the user confirms, and the two can go back and forth. The proposal is a **dinner table** in the channel, updated each time the user asks for a change ("swap Thursday for something with fish", "make Friday new"). The draft is stored in SQLite (`meal_draft`) and the table is regenerated from it. Only a confirmation writes to Mealie. My call: confirmation is a **Discord button ("Confirmer") under the table**, handled by the bot, not a model decision. The Mealie writes (create the new recipes, set the meal plan, build the shopping list) run in code from the stored draft when the button is pressed, so the model cannot write to Mealie on its own. Typing "ok" does not commit; the button does. Read-only Mealie calls (search, get recipe) need no confirmation. **The button disappears once used** (decided by the user): when pressed, the bot first removes the button from the message (so a double click cannot commit twice), then commits, then edits the message to show the outcome ("Confirmé, enregistré dans Mealie" or the error with the button restored if the commit failed). The same applies to superseded tables: when a changed table is re-posted, the button is removed from the previous table message, so only the latest draft is ever confirmable.
5d. **Table format** (Discord does not render markdown tables, so a monospace code block):

```
Date      Entraînement  Dîner       Recette                        Déj. lendemain
Lun 23/11 rameur        protéines   Poulet rôti, courge (existante) Mar: reste
Mar 24/11 repos         veggie      Curry de pois chiches (NOUVELLE) Mer: reste
Mer 25/11 HIIT          protéines   Saumon, lentilles (existante)   Jeu: reste
Jeu 26/11 marche        veggie      — absent(e) —                   Ven: vide
```
   The agent can show the full draft of a new recipe on request before the user confirms.
6. **Fridge**: stored in SQLite (`fridge_items`: name, quantity as free text, updated_at). Tools to add, remove and replace items. The shopping list excludes ingredients the fridge already covers (the agent judges coverage; exact units are not tracked).
7. **Guidelines in the prompt** (from the history): protein in every meal, vegetables freely, no forbidden foods, seasonal French produce (the agent knows today's date), modest deficit 300-500 kcal under need, no extreme restriction, weigh at most once a week.
8. **Shared agent runtime**: extract the run loop from `app/coach.py` into a reusable module so both agents use one implementation (prompt file + tool list + channel key). The coach's behavior stays unchanged.
9. **Tools the model can call**: `get_meal_targets`, `get_fridge`, `set_fridge`, `add_fridge_items`, `remove_fridge_items`, `set_away_days`, `get_away_days`, `get_profile`, `search_recipes` and `get_recipe` (read-only Mealie), and the draft tools `set_draft_dinner` (existing recipe or new recipe with ingredients and steps, always 2 portions), `get_draft`, `clear_draft`. **No Mealie write tool is exposed to the model.** The bot commits the draft to Mealie (create recipes, meal plan entries, shopping list) when the user presses the confirm button. Tool names are indicative.

## Flow

```
Sunday 10:00  ─► post: "Which days next week are you away? Anything new in the fridge?"
              + "How many new recipes this week?"
user replies  ─► set_away_days / set_fridge / new-recipe count
              ─► get_meal_targets (reads coach plan + away days)
              ─► for each cooking evening: existing Mealie recipe of the right type,
                 or a new draft recipe (as many new ones as the user asked)
              ─► post the dinner table + [Confirmer] button
user: "swap Thursday…"  ─► draft updated ─► table re-posted        (repeat as needed)
user presses [Confirmer] ─► bot writes to Mealie: new recipes, meal plan
                            (dinner + next-day leftover lunch), shopping list minus fridge
                         ─► short confirmation in #nutrition
```

## Left to the builder

- Exact Mealie endpoints, auth header and recipe/meal plan/shopping list payloads, taken from the instance's `/docs`.
- How recipes are tagged or categorized in Mealie so that protein and veggie recipes can be searched; the convention is the French tags `protéines` and `végétarien` (internal dinner types stay `protein` / `veggie`); if the user's Mealie already uses other tags, ask once and adapt.
- How leftover lunches appear in the Mealie plan (same recipe on the next day's lunch slot).
- Shopping list de-duplication and fridge subtraction details.
- Retry and error messages when Mealie is unreachable, and what happens if the commit fails halfway (the draft must stay intact so the user can press confirm again; avoid duplicate recipes on retry).
- The `meal_draft` schema and how a button press is bound to a specific draft version (a stale button on an older table must not commit a changed draft).
- Tests: the rules in `get_meal_targets` (exercise vs non-exercise, away-day effects on lunches) are pure functions and should be tested; the draft-to-Mealie commit and the Mealie client should be tested against a mocked server.

## Open items

None blocking. The user confirmed 2 portions per recipe, always.
