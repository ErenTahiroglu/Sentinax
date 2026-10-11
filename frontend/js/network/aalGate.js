/**
 * Client-side AAL gate. UX/defence-in-depth ONLY: getAuthenticatorAssuranceLevel() reads the locally
 * held session, so the server (JWT verifier, owner dependency, RLS) remains the sole authority.
 * Fails closed on any error.
 */
export async function isAal2Session(auth) {
    try {
        const { data, error } = await auth.mfa.getAuthenticatorAssuranceLevel();
        return !error && data?.currentLevel === 'aal2';
    } catch {
        return false;
    }
}
