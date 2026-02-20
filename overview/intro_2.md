---

## title: Introduction

---

### What is Retab?

Retab solves all the major challenges in data processing with Large Language Models:

1. **Parsing**: Convert any file type (PDFs, Excel, emails, etc.) into LLM-ready format without writing custom parsers
2. **Extraction**: Get consistent, reliable outputs using schema-based prompt engineering
3. **Projects**: Evaluate the performance of models against annotated datasets
4. **Deployments**: Publish a live, stable, shareable document processor from your project.


We are offering you all the software-defined primitives to build your own document processing solutions. We see it as **Stripe** for document processing.

Our goal is to make the process of analyzing documents and unstructured data as **easy** and **transparent** as possible.




### A new, lighter paradigm

Large Language Models collapse entire layers of legacy OCR pipelines into a single, elegant abstraction. When a model can **read**, **reason**, and **structure text** natively, we no longer need brittle heuristics, handcrafted parsers, or heavyweight ETL jobs. 

Instead, we can expose a **small, principled API**: input your document, define the output schema, and receive reliable structured data. This reduces complexity, improves accuracy, speeds up processing, and lowers costs. By building around LLMs from the ground up, we shift the focus from tedious infrastructure to extracting meaningful answers from your data.


Many people haven't yet realized how powerful LLMs have become at document processing tasks. We believe that LLMs and structured generation are among the most **impactful breakthroughs** of the 21th century. [AI is the new electricity](https://www.answer.ai/posts/2023-12-12-launch.html), and retab is here to help you tame it.


---

## Structured Generation

JSON is one of the most widely used formats in the world for applications to exchange data.

Structured Generation is a feature that ensures the AI model will always generate responses that adhere to your supplied [JSON Schema](https://json-schema.org/), so you don't need to worry about the model omitting a required key, or hallucinating an invalid enum value.


<Accordion title="How to Use Structured Generation ">


Every LLM service providers native structured generation support.

<CodeGroup>
```python python
from pydantic import BaseModel
from openai import OpenAI

client = OpenAI()

class ResearchPaperExtraction(BaseModel):
    title: str
    authors: list[str]
    abstract: str
    keywords: list[str]

completion = client.completions.parse(
    json_schema=ResearchPaperExtraction.model_json_schema(),
    messages=[
        {"role": "system", "content": "You are an expert at structured data extraction. You will be given unstructured text from a research paper and should convert it into the given structure."},
        {"role": "user", "content": "..."}
    ],
    model="gpt-5",
    temperature=0
)
```

```javascript Javascript
import OpenAI from "openai";
import { zodResponseFormat } from "openai/helpers/zod";
import { z } from "zod";

const openai = new OpenAI();

const ResearchPaperExtraction = z.object({
  title: z.string(),
  authors: z.array(z.string()),
  abstract: z.string(),
  keywords: z.array(z.string()),
});

const completion = await openai.chat.completions.parse({
  model: "gpt-5.2",
  messages: [
    { role: "system", content: "You are an expert at structured data extraction. You will be given unstructured text from a research paper and should convert it into the given structure." },
    { role: "user", content: "..." },
  ],
  response_format: zodResponseFormat(ResearchPaperExtraction, "research_paper"),
});

const researchPaper = completion.choices[0].message.parsed;
```

```json json_schema.json
{
  "properties": {
    "title": {
      "title": "Title",
      "type": "string"
    },
    "authors": {
      "items": {
        "type": "string"
      },
      "title": "Authors",
      "type": "array"
    },
    "abstract": {
      "title": "Abstract",
      "type": "string"
    },
    "keywords": {
      "items": {
        "type": "string"
      },
      "title": "Keywords",
      "type": "array"
    }
  },
  "required": [
    "title",
    "authors",
    "abstract",
    "keywords"
  ],
  "title": "ResearchPaperExtraction",
  "type": "object"
}
```
```json output.json
{
  "title": "The Impact of Climate Change on Global Agriculture",
  "authors": [
    "John Doe",
    "Jane Smith"
  ],
  "abstract": "This paper explores the effects of climate change on global agriculture, examining how rising temperatures and changing precipitation patterns are impacting crop yields and food security.",
  "keywords": [
    "climate change",
    "global agriculture",
    "food security"
  ]
}
```
</CodeGroup>

Usage involves defining a schema for your desired output and including it in your API request. The schema can be a JSON Schema document or a data model class (like Pydantic BaseModel) that SDKs convert to JSON Schema. The LLM generates responses conforming to that schema, eliminating the need for post-processing or complex prompt engineering.

</Accordion>

## Community

Let's create the future of document processing together!

Join our [discord community](https://discord.com/invite/vc5tWRPqag) to share tips, discuss best practices, and showcase what you build. Or just [tweet](https://x.com/retabdev) at us.

We can't wait to see how you'll use Retab.

* [Discord](https://discord.com/invite/vc5tWRPqag)
* [Twitter](https://x.com/retabdev)

## Roadmap

We share our roadmap publicly. Please submit your feature requests on [Github](https://github.com/retab-dev/retab)

Among the features we're working on:

* [ ] Schema optimization autopilot
* [ ] Sources API
* [ ] Document Edit API

---
## Learn More

* [OpenAI](https://platform.openai.com/docs/guides/structured-outputs), [Google](https://ai.google.dev/gemini-api/docs/structured-output), [xAI](https://docs.x.ai/docs/guides/structured-outputs), [Outlines](https://dottxt-ai.github.io/outlines/latest/reference/generation/structured_generation_explanation/) on structured generation
* [Structured generation Starter Pack](https://github.com/retab-dev/structured-generation-starter-pack)
* [Quickstart](/get-started/quickstart)
* [API Reference](/api-reference/introduction)
* [Github Repository](https://github.com/retab-dev/retab)

---
