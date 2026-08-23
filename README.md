# Simple Harness Service SDK

Thin authenticated service and transport framework for Simple Harness SDK. Harness remains the
only durable command and execution authority; this package owns no database or execution worker.

Version 0.1.2 pins Harness 0.5.1 so product adapters can bind durable starts to an exact Tool
catalog fingerprint. It retains the 0.1.1 interactive chat lifecycle behavior.
