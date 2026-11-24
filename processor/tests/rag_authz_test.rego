package rag.authz

import rego.v1

# Test: User with correct role is allowed to see the chunk
test_user_with_correct_role_allowed if {
    allow with input as {
        "user": {"roles": ["engineering"]},
        "document": {"metadata": {"allowed_roles": ["engineering", "product"]}}
    }
}

# Test: User with wrong role is denied the chunk
test_user_with_wrong_role_denied if {
    not allow with input as {
        "user": {"roles": ["marketing"]},
        "document": {"metadata": {"allowed_roles": ["engineering", "product"]}}
    }
}

# Test: Admin is always allowed regardless of document tags
test_admin_always_allowed if {
    allow with input as {
        "user": {"roles": ["admin"]},
        "document": {"metadata": {"allowed_roles": ["finance"]}}
    }
}