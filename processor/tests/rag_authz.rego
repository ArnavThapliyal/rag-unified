package rag.authz

import rego.v1

# Default to denying access
default allow := false

# Allow if the user's role matches one of the document's allowed roles
allow if {
    some i, j
    input.user.roles[i] == input.document.metadata.allowed_roles[j]
}

# Admins bypass all document restrictions
allow if {
    some i
    input.user.roles[i] == "admin"
}