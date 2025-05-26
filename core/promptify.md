


## Promptify Schema

The Promptify Schema endpoint allows you to enhance an existing JSON Schema with AI-generated prompts and descriptions. By analyzing example documents, it can add helpful X-Directives (`X-SystemPrompt` and `X-ReasoningPrompt`) that will guide the AI in extracting the right information.

This is particularly useful when you already have a schema structure but want to improve the quality of AI extractions by adding more context and guidance through prompts.

<CodeGroup>
```python Request
from uiform import UiForm

uiclient = UiForm()

schema_obj = uiclient.schemas.promptify(
    json_schema = {
      'properties': {
          'name': {
              'description': 'The name of the calendar event.',
              'title': 'Name',
              'type': 'string'
          },
          'date': {
              'description': 'The date of the calendar event in ISO 8601 format.',
              'title': 'Date',
              'type': 'string'
          }
      },
      'required': ['name', 'date'],
      'title': 'CalendarEvent',
      'type': 'object'
    },
    modality = "native",
    model = "gpt-4.1",
    temperature = 0,
    stream = False,
    documents = [
        "freight/booking_confirmation_1.jpg",
        "freight/booking_confirmation_2.jpg"
    ]
)
```
```json Response
{
    "id_": "sch_id_c547dbfaa0cbcb8d646ca7c53af54d8870f2740e",
    "object": "schema",
    "created_at": "2024-01-01T00:00:00Z",
    "json_schema": {
        "X-SystemPrompt": "You are a useful assistant.",
        "properties": {
            "name": {
                "description": "The name of the calendar event.",
                "title": "Name",
                "type": "string"
            },
            "date": {
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
    },
    "data_id": "sch_data_id_d6d04390f2390eab3dc9a017d043b26b95faeb94",
}
```

</CodeGroup>



## System Prompt

The Generate System Prompt endpoint allows you to create an optimized system prompt for your JSON Schema based on example documents. This helps guide the AI to better understand the context and requirements when extracting information.

```python
from uiform import UiForm

uiclient = UiForm()

system_prompt = uiclient.schemas.system_prompt(
    json_schema = {
      'properties': {
          'name': {
              'description': 'The name of the calendar event.',
              'title': 'Name',
              'type': 'string'
          },
          'date': {
              'description': 'The date of the calendar event in ISO 8601 format.',
              'title': 'Date',
              'type': 'string'
          }
      },
      'required': ['name', 'date'],
      'title': 'CalendarEvent',
      'type': 'object'
    },
    documents = [
        "freight/booking_confirmation_1.jpg",
        "freight/booking_confirmation_2.jpg"
    ],
    model = "gpt-4.1",
    temperature = 0,
    modality = "native",
    stream = False,
)
```
