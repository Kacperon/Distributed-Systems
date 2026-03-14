from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Dict

# Task 1 – Doodle API example
# • Create simple Doodle API
# • Small API for voting
#1 – User can create poll (see what is insider poll)
#2 – User can cast a vote inside this polls
#3 – User can add, update and delete all information
# he provides
#4 – User can see the results of votes
# • Construct API and build the system
# – Test it with the Swagger UI

app = FastAPI()

polls = {}
votes = {}
n=1
class Poll(BaseModel):
	question: str
	options: List[str]

class Vote(BaseModel):
	option: str

# Create poll 1
@app.post("/polls")
def create_poll(poll: Poll):
	global n
	poll_id = str(n+1)
	polls[poll_id] = poll.model_dump()
	votes[poll_id] = {}
	n += 1 
	return {"poll_id": poll_id}

# Get poll 
@app.get("/polls/{poll_id}")
def get_poll(poll_id: str):
	return polls[poll_id]

# Update poll 3
@app.put("/polls/{poll_id}")
def update_poll(poll_id: str, poll: Poll):
	polls[poll_id] = poll.model_dump()
	return polls[poll_id]

# Delete poll 3
@app.delete("/polls/{poll_id}")
def delete_poll(poll_id: str):
	polls.pop(poll_id)
	votes.pop(poll_id)
	return {"ok": True}

# Vote 2
@app.post("/polls/{poll_id}/vote")
def cast_vote(poll_id: str, vote: Vote):
	v = votes[poll_id]
	options = polls[poll_id]["options"]
	if vote.option not in options:
		return {"ok": False, "msg": "Invalid option", "available_options": options}
	v[vote.option] = v.get(vote.option, 0) + 1
	return {"ok": True}

# Get results 4
@app.get("/polls/{poll_id}/results")
def get_results(poll_id: str):
	return votes[poll_id]
