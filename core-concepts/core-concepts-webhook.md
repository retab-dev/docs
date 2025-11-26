
----

## Webhooks

Retab uses HTTPS to send webhook events to your app as a JSON payload representing a `WebhookRequest` object.

The `WebhookRequest` object contains the following fields:

- `completion`: The parsed chat completion object, containing the extracted data.
- `user`: The user email address.
- `file_payload`: The file payload object, containing the file name, url and other metadata.
- `metadata`: Some additional metadata.


<CodeGroup>
```python Python
from openai.types.chat.parsed_chat_completion import ParsedChatCompletion

class MIMEData(BaseModel):
    filename: str = Field(description="The filename of the file", examples=["file.pdf", "image.png", "data.txt"])
    url: str = Field(description="The URL of the file in base64 format", examples=["data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADIA..."])
    ##... other fields

class WebhookRequest(BaseModel):
    completion: ParsedChatCompletion
    user: Optional[EmailStr] = None
    file_payload: MIMEData
    metadata: Optional[dict[str, Any]] = None

```

```typescript Node.js
import { ParsedChatCompletion } from 'openai/resources/chat/completions';

interface MIMEData {
    filename: string; // The filename of the file, e.g., "file.pdf", "image.png", "data.txt"
    url: string; // The URL of the file in base64 format, e.g., "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADIA..."
    // ... other fields
}

interface WebhookRequest {
    completion: ParsedChatCompletion;
    user?: string; // User email address
    file_payload: MIMEData;
    metadata?: Record<string, any>;
}

```
</CodeGroup>

To start receiving webhook events in your app:

- Create a webhook endpoint handler to receive event data POST requests.
- Create a new deployment sending data to your webhook endpoint.
- Test your webhook endpoint handler locally using the Retab SDK.
- Secure your webhook endpoint.


### Create a webhook endpoint handler

Set up an HTTPS endpoint function that can accept webhook requests with a POST method.

Set up your endpoint function so that it:

- Handles POST requests with a JSON payload consisting of an event object.
- Quickly returns a successful status code (2xx) prior to any complex logic that might cause a timeout.

<CodeGroup>
```python Python
import os

from fastapi import FastAPI, Request, Response, HTTPException
from retab import Retab

client = Retab()
app = FastAPI()

@app.post("/webhook")
async def webhook_handler(request: Request):

    payload = await request.body()

    # Decode and parse the webhook request from the verified payload
    json_data = json.loads(payload.decode('utf-8'))
    webhook_request = WebhookRequest.model_validate(json_data)

    invoice_object = Invoice.model_validate_json(webhook_request.completion.choices[0].message.content or "{}") # The parsed object is the same Invoice object as the one you defined in the Pydantic model
    print("📬 Webhook received:", invoice_object)
    return {"status": "success", "data": invoice_object}


# To run the FastAPI app locally, use the command:
# uvicorn your_module_name:app --reload
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

```javascript Node.js
import express from 'express';

const app = express();

// Use express.raw() to get the raw body for signature verification
app.use(express.raw({ type: 'application/json' }));

app.post('/webhook', async (req, res) => {
    const payload = req.body;

    // Decode and parse the webhook request from the verified payload
    const webhookRequest = JSON.parse(payload.toString('utf-8'));

    const invoiceObject = JSON.parse(
        webhookRequest.completion.choices[0].message.content || '{}'
    ); // The parsed object matches your Invoice schema
    console.log('📬 Webhook received:', invoiceObject);
    
    res.status(200).json({ status: 'success', data: invoiceObject });
});

// To run the Express app locally:
// node your_module_name.js
app.listen(8000, () => {
    console.log('Webhook server listening on port 8000');
});
```
</CodeGroup>

### Create a new deployment sending data to your webhook endpoint

Let's create a new processor and automation to send data to your webhook endpoint.

First, create a processor:

<CodeGroup>
```python Python
class Invoice(BaseModel):
    amount: int
    currency: str
    customer_email: EmailStr


from retab import Retab

client = Retab()

# Step 1: Create a processor
processor = client.processors.create(
    name="Invoice Processor",
    model="gpt-4.1",
    json_schema=Invoice.model_json_schema(),
)

# Step 2: Create an automation and attach it to the processor
extraction_link = client.processors.automations.links.create(
    name="Invoices",
    processor_id=processor.id,
    webhook_url="https://your_server.com/invoices/webhook",
)
```

```javascript Node.js
import Retab from 'retab';

// Define your Invoice schema
const invoiceSchema = {
    type: 'object',
    properties: {
        amount: { type: 'integer' },
        currency: { type: 'string' },
        customer_email: { type: 'string', format: 'email' }
    },
    required: ['amount', 'currency', 'customer_email']
};

const client = new Retab();

// Step 1: Create a processor
const processor = await client.processors.create({
    name: 'Invoice Processor',
    model: 'gpt-4.1',
    json_schema: invoiceSchema,
});

// Step 2: Create an automation and attach it to the processor
const extractionLink = await client.processors.automations.links.create({
    name: 'Invoices',
    processor_id: processor.id,
    webhook_url: 'https://your_server.com/invoices/webhook',
});
```
</CodeGroup>

### Test your webhook endpoint handler locally using the Retab SDK

Before you go-live with your webhook endpoint function, we recommend that you test your application integration.

You can do so by sending a test request to your webhook endpoint using the Retab SDK.

<CodeGroup>
```python Python
from retab import Retab

client = Retab()
link_log = client.processors.automations.links.test_document_upload(
    link_id=extraction_link.id,
    document = "invoice.pdf"
)
```

```javascript Node.js
import Retab from 'retab';

const client = new Retab();
const linkLog = await client.processors.automations.links.testDocumentUpload({
    link_id: extractionLink.id,
    document: 'invoice.pdf'
});
```
</CodeGroup>

### Secure your webhook endpoint

You need to secure your integration by making sure your handler verifies that all webhook requests are generated by Retab. 

You perform the verification by providing the event payload, the `Retab-Signature` header, and the endpoint's secret. If verification fails, you get an error.


**Important:** Make sure to replace `"your_webhooks_secret_here"` with the actual secret from your [Retab dashboard](https://www.retab.com/dashboard/settings).

<CodeGroup>
```python Python
import os

from fastapi import FastAPI, Request, Response, HTTPException
from retab import Retab

client = Retab()
app = FastAPI()

@app.post("/webhook")
async def webhook_handler(request: Request):

    payload = await request.body()

    #########################################################
    # SIGNATURE VERIFICATION
    #########################################################
    try:
        # Read the signature from the request headers
        
        signature_header = request.headers.get("Retab-Signature")

        if not signature_header:
            raise HTTPException(status_code=400, detail="Missing Retab-Signature header")

        # Verify the signature and process the event
        client.verify_event(
            event_body=payload,
            event_signature=signature_header,
            secret=os.getenv("WEBHOOKS_SECRET"),
        )

        return Response(status_code=200)
    except Exception as e:
        # Handle errors (optional: log the error)
        return Response(status_code=400, content=f"Signature verification error. Please verify your signature secret. {str(e)}")

    ########################################################   
    # END OF SIGNATURE VERIFICATION
    #########################################################

    # Decode and parse the webhook request from the verified payload
    json_data = json.loads(payload.decode('utf-8'))
    webhook_request = WebhookRequest.model_validate(json_data)

    #Then continue with your webhook handler logic .....
    invoice_object = Invoice.model_validate_json(webhook_request.completion.choices[0].message.content or "{}") # The parsed object is the same Invoice object as the one you defined in the Pydantic model
    print("📬 Webhook received:", invoice_object)
    return {"status": "success", "data": invoice_object}


```

```javascript Node.js
import express from 'express';
import Retab from 'retab';

const client = new Retab();
const app = express();

// Use express.raw() to get the raw body for signature verification
app.use(express.raw({ type: 'application/json' }));

app.post('/webhook', async (req, res) => {
    const payload = req.body;

    //////////////////////////////////////////////////////////
    // SIGNATURE VERIFICATION
    //////////////////////////////////////////////////////////
    try {
        // Read the signature from the request headers
        const signatureHeader = req.headers['retab-signature'];

        if (!signatureHeader) {
            return res.status(400).json({ detail: 'Missing Retab-Signature header' });
        }

        // Verify the signature and process the event
        await client.verifyEvent({
            event_body: payload,
            event_signature: signatureHeader,
            secret: process.env.WEBHOOKS_SECRET,
        });

    } catch (error) {
        // Handle errors (optional: log the error)
        return res.status(400).send(`Signature verification error. Please verify your signature secret. ${error.message}`);
    }

    //////////////////////////////////////////////////////////
    // END OF SIGNATURE VERIFICATION
    //////////////////////////////////////////////////////////

    // Decode and parse the webhook request from the verified payload
    const webhookRequest = JSON.parse(payload.toString('utf-8'));

    // Then continue with your webhook handler logic .....
    const invoiceObject = JSON.parse(
        webhookRequest.completion.choices[0].message.content || '{}'
    ); // The parsed object matches your Invoice schema
    console.log('📬 Webhook received:', invoiceObject);
    
    return res.status(200).json({ status: 'success', data: invoiceObject });
});
```
</CodeGroup>

#### Vulnerability

When you set up a webhook, you provide an **HTTP endpoint** on your server for Retab to send data to. If this endpoint is not secured (i.e., it accepts unauthenticated `POST` requests from anywhere), it essentially becomes a public door into your system. **Any actor** could attempt to call this URL and send fake data. This is inherently dangerous: a malicious party might send **forged webhook requests** that masquerade as Retab, but contain bogus or harmful data. Without verification, your server might accept these fake events and perform unintended actions. In short, an open webhook endpoint without authentication is **highly vulnerable** to abuse.

#### Mitigation

To secure webhook deliveries, Retab employs a **signature verification** mechanism using an HMAC-like scheme (similar to the approach used by [Stripe](https://docs.stripe.com/webhooks#verify-official-libraries), [WorkOS](https://workos.com/docs/events/data-syncing/webhooks/1-set-up-your-webhook-endpoint), and other providers). HMAC stands for *Hash-Based Message Authentication Code*, a method that uses a secret key to create a cryptographic signature for each message. In fact, HMAC signing is by far the most popular strategy for webhook security (used in \~65% of webhook implementations), because it ensures that only the trusted source (with knowledge of the secret) could have sent the request.

**How it works:** Retab and your application share a **webhook secret** (a random string known only to Retab and you). This secret is available in your Retab dashboard (often labeled as `WEBHOOKS_SECRET`). Retab uses this secret to include a special signature header with every webhook request:

* **Signature Generation (Retab):** When Retab prepares to send a webhook, it computes an HMAC-SHA256 signature of the webhook's payload using your secret key, and includes this signature in the request headers (specifically in the `Retab-Signature` header).
* **Signature Verification (Your Server):** When your endpoint receives the webhook, your code should perform the same HMAC-SHA256 computation on the request body using the shared secret. Then, compare your computed signature to the value in the `Retab-Signature` header.
* **Comparison Result:** If the two signatures **match**, it means the request truly came from Retab (since only Retab knows the secret) and that the payload was not altered in transit. You can then safely trust and process the webhook. If the signatures **do not match**, the request is illegitimate – either coming from an impostor or corrupted – and you should reject it (e.g. respond with an HTTP 400 Bad Request or 401 Unauthorized).

By using this HMAC signature validation, we achieve both **authentication** and **integrity** for webhook messages: only the genuine sender (Retab) can produce a matching signature, and any change to the data would break the signature verification. In other words, a valid `Retab-Signature` proves that the webhook content is exactly what Retab sent and has not been tampered with.



#### Code Snippets
---
Here are some code snippets for different frameworks.

<CodeGroup>
```python FastAPI (Python)
import os

from fastapi import FastAPI, Request, Response, HTTPException
from retab import Retab

client = Retab()
app = FastAPI()

@app.post("/webhook")
async def webhook_handler(request: Request):
    try:
        # Read payload and signature from request
        payload = await request.body()
        signature_header = request.headers.get("Retab-Signature")

        if not signature_header:
            raise HTTPException(status_code=400, detail="Missing Retab-Signature header")

        # Verify the signature and process the event
        client.verify_event(
            event_body=payload,
            event_signature=signature_header,
            secret=os.getenv("WEBHOOKS_SECRET"),
        )

        return Response(status_code=200)
    except Exception as e:
        # Handle errors (optional: log the error)
        return Response(status_code=400, content=f"Signature verification error. Please verify your signature secret. {str(e)}")
```

```python Django (Python)
import os

from django import Response
from retab import Retab

client = Retab()

def webhook_handler(request):
    payload = request.get_data()
    signature_header = request.headers["Retab-Signature"]

    # Verify the signature and process the event
    response = client.verify_event(
        event_body=payload,
        event_signature=signature_header,
        secret=os.getenv("WEBHOOKS_SECRET"),
    )

    return Response(status=200)
```

```python Flask (Python)
import os

from flask import Flask, Response, request
from retab import Retab

client = Retab()

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook_handler():
    payload = request.get_data()
    signature_header = request.headers["Retab-Signature"]

    # Verify the signature and process the event
    response = client.verify_event(
        event_body=payload,
        event_signature=signature_header,
        secret=os.getenv("WEBHOOKS_SECRET"),
    )

    return Response(status=200)
```

```javascript Express (Node.js)
import express from 'express';
import Retab from 'retab';

const client = new Retab();
const app = express();

app.use(express.raw({ type: 'application/json' }));

app.post('/webhook', async (req, res) => {
    try {
        const payload = req.body;
        const signatureHeader = req.headers['retab-signature'];

        if (!signatureHeader) {
            return res.status(400).json({ detail: 'Missing Retab-Signature header' });
        }

        // Verify the signature and process the event
        await client.verifyEvent({
            event_body: payload,
            event_signature: signatureHeader,
            secret: process.env.WEBHOOKS_SECRET,
        });

        return res.status(200).send();
    } catch (error) {
        return res.status(400).send(`Signature verification error. Please verify your signature secret. ${error.message}`);
    }
});
```

```javascript Fastify (Node.js)
import Fastify from 'fastify';
import Retab from 'retab';

const client = new Retab();
const fastify = Fastify();

fastify.post('/webhook', async (request, reply) => {
    try {
        const payload = request.rawBody;
        const signatureHeader = request.headers['retab-signature'];

        if (!signatureHeader) {
            return reply.status(400).send({ detail: 'Missing Retab-Signature header' });
        }

        // Verify the signature and process the event
        await client.verifyEvent({
            event_body: payload,
            event_signature: signatureHeader,
            secret: process.env.WEBHOOKS_SECRET,
        });

        return reply.status(200).send();
    } catch (error) {
        return reply.status(400).send(`Signature verification error. Please verify your signature secret. ${error.message}`);
    }
});
```

```javascript Next.js (Node.js)
import Retab from 'retab';

const client = new Retab();

export async function POST(request) {
    try {
        const payload = await request.arrayBuffer();
        const signatureHeader = request.headers.get('retab-signature');

        if (!signatureHeader) {
            return new Response(
                JSON.stringify({ detail: 'Missing Retab-Signature header' }),
                { status: 400 }
            );
        }

        // Verify the signature and process the event
        await client.verifyEvent({
            event_body: Buffer.from(payload),
            event_signature: signatureHeader,
            secret: process.env.WEBHOOKS_SECRET,
        });

        return new Response(null, { status: 200 });
    } catch (error) {
        return new Response(
            `Signature verification error. Please verify your signature secret. ${error.message}`,
            { status: 400 }
        );
    }
}
```
</CodeGroup>
