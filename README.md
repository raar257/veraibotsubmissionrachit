# Vera Bot — My Submission

## Approach
I built a rule-based message engine (no live AI/LLM call at runtime). For each 
type of trigger — sales dip, sales spike, festival coming up, customer hasn't 
visited in a while, review pattern, milestone reached, etc. — I wrote a template 
that pulls real numbers and facts straight from the merchant's data (calls, 
views, offers, review counts, visit history) and fills them into the message. 
So every message is grounded in actual data for that specific shop, not 
generic text.

## Why I chose this over calling an AI API
- No API key needed, no cost per message
- Never times out or hits a rate limit — which matters a lot since the judge 
  has a strict time limit per response
- 100% predictable — the same input always gives the same output, so it's 
  easy to test and debug
- No risk of the AI making up facts that aren't actually true about the merchant

## Trade-off I'm aware of
Since it's template-based, messages for the same type of situation (e.g. two 
different dentists both facing a sales dip) follow a similar sentence 
structure — just with different numbers filled in. A live AI call could write 
more varied, natural-sounding text each time, but I prioritized reliability 
and factual accuracy over writing variety.

## How it handles conversations
- If the merchant says "yes, let's do it" — it moves straight to action instead 
  of asking more questions
- If it detects the merchant is sending an auto-reply (same canned text 
  repeated), it stops trying after one gentle check-in
- If the merchant seems annoyed or asks something unrelated, it responds 
  politely and doesn't push
- It never repeats the exact same message twice in one conversation

## Files
- `bot.py` — the server that receives requests and sends responses
- `composer.py` — the logic that decides what message to write
- `submission.jsonl` — sample outputs for the 30 test cases
- `requirements.txt`, `Dockerfile`, `render.yaml` — for deployment