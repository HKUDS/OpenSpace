# Third-party notices

RSI Arena's OpenEvolve and ShinkaEvolve baseline runtimes are adapted implementations derived from the following projects:

- OpenEvolve, https://github.com/algorithmicsuperintelligence/openevolve, commit `411fb59c886c18704caaffb611e17cf9e7d824d2`.
- ShinkaEvolve, https://github.com/SakanaAI/ShinkaEvolve, commit `b67a07328ab7e21e999d9e20a44f4f0054a4b83c`.

Both upstream projects are licensed under the Apache License 2.0. The license text is provided once in `APACHE-2.0.txt`. RSI Arena changes the execution and evaluation boundaries so these runtimes submit candidates through Arena, persist Arena receipts, and recover interrupted evaluations without exposing trusted benchmark state to the search loop.
