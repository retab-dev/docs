from uiform import UiForm
from openai import OpenAI
from pydantic import BaseModel, Field

uiclient = UiForm()

doc_msg = uiclient.documents.create_messages(
    document = "freight/booking_confirmation.jpg"
)

json_schema = {
  "X-SystemPrompt": "You are a useful assistant extracting information from documents.",
  "properties": {
    "name": {
      "description": "The name of the calendar event.",
      "title": "Name",
      "type": "string"
    },
    "date": {
      "X-ReasoningPrompt": "The user can mention it in any format, like **next week** or **tomorrow**. Infer the right date format from the user input.",
      "description": "The date of the calendar event in ISO 8601 format.",
      "title": "Date",
      "type": "string"
    }
  },
  "required": [
    "name",
    "date"
  ],
  "title": "CalendarEvent",
  "type": "object"
}


schema_obj = uiclient.schemas.load(json_schema = json_schema)

# Now you can use your favorite model to analyze your document
client = OpenAI()
completion = client.chat.completions.create(
    model="gpt-4o",
    messages=schema_obj.openai_messages + doc_msg.openai_messages,
    response_format=schema_obj.response_format.json_schema
)

print("Extraction:",completion.choices[0].message.content)

schema_obj.pydantic_model.model_validate_json(
    completion.choices[0].message.content
)





# Now you can use your favorite model to analyze your document
client = OpenAI()
completion = client.chat.completions.create(
    model="gpt-4o",
    messages=schema_obj.openai_messages + doc_msg.openai_messages,
    response_format=schema_obj.response_format.pydantic
)

print("Extraction:",completion.choices[0].message.content)

# To get your object, you still need to validate the json
schema_obj.pydantic.model_validate_json(
    completion.choices[0].message.content
)


# Now you can use your favorite model to analyze your document
client = OpenAI()
completion = client.chat.completions.create(
    model="gpt-4o",
    messages=schema_obj.openai_messages + doc_msg.openai_messages,
    response_format=schema_obj.response_format.json_schema 
)

print("Extraction:",completion.choices[0].message.content)

# To get your object, you still need to validate the json
schema_obj.pydantic_basemodel.model_validate_json(
    completion.choices[0].message.content
)