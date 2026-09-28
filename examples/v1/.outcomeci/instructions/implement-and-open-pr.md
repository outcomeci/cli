Implement your repository's part of the approved plan, then open one pull
request. `target` is your repository and its steps; the full plan is context
for how your change fits the other repositories' changes.

1. Read the files your steps name, and any others you need to make the change
   correctly.
2. Create a branch from the default branch's current tip, named
   `slack-feature/<short-id>`.
3. Make the smallest change that does exactly what your steps say. It may span
   several files; do not refactor or restyle anything they do not cover.
4. Commit each changed file to the new branch.
5. Open a pull request against the default branch. Its body contains, in order:
   - What was requested, in one or two sentences.
   - Your repository's steps from the approved plan, and which other
     repositories are changing alongside it.
   - What changed, file by file.
   - How to verify in staging: a numbered, literal procedure taken from what
     the repository actually provides (README, CI and deploy workflows,
     Makefile or package scripts). State what is true; do not invent a
     mechanism the repository lacks.

If the repository does not match what your steps assume, stop without opening
a pull request and return the reason. Never open a partial or guessed change.
