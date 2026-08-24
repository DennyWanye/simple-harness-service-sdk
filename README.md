# Simple Harness Service SDK

Thin authenticated service and transport framework for Simple Harness SDK. Harness remains the
only durable command and execution authority; this package owns no database or execution worker.

Version 0.1.3 pins Harness 0.5.2 so nested durable start input is correctly thawed before
`RunStart` construction. It retains the 0.1.2 API, stateless architecture, and interactive chat
lifecycle behavior.
