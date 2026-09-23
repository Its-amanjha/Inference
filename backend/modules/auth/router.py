import ipaddress
import secrets
import time

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
import pyotp

from backend import audit
from backend.auth_gate import CSRF_COOKIE, SESSION_COOKIE, constant_equal, fail, public_user, require_role, require_current_role, token_hash, valid_csrf
from .service import consume_factor, new_session, password_hash, password_matches, set_csrf_cookie, throttle

router = APIRouter()


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1,max_length=64,pattern=r'^[A-Za-z0-9_.-]+$')
    password: str = Field(min_length=1,max_length=256)
    code: str = Field(default='',max_length=64)


class SetupCredentials(Credentials):
    password: str = Field(min_length=14,max_length=256)


class PasswordCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=1,max_length=256)


class Factor(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(min_length=6,max_length=64)


class DisableFactor(PasswordCheck):
    code: str = Field(min_length=6,max_length=64)


@router.get('/status')
async def status(request: Request, response: Response) -> dict:
    user=getattr(request.state,'user',None)
    if user and not valid_csrf(user,request.cookies.get(CSRF_COOKIE,'')):
        set_csrf_cookie(request,response,user)
    exists=await request.app.state.db.fetchone('SELECT username FROM users LIMIT 1')
    return {'user':public_user(user),'setup_required':exists is None}


def check_bootstrap(request: Request) -> None:
    expected=request.app.state.settings.bootstrap_token
    if expected:
        supplied=request.headers.get('X-Inference-Bootstrap','')
        if not constant_equal(supplied,expected):
            fail(403,'bootstrap_required','Supply the configured bootstrap token.')
        return
    try:
        local_client=bool(request.client) and ipaddress.ip_address(request.client.host).is_loopback
        host=request.url.hostname
        local_host=host=='localhost' or ipaddress.ip_address(host or '').is_loopback
    except ValueError:
        local_client=local_host=False
    if not local_client or not local_host:
        fail(403,'bootstrap_required','Create the first account locally or configure INFERENCE_BOOTSTRAP_TOKEN.')


@router.post('/setup')
async def setup(body: SetupCredentials, request: Request, response: Response) -> dict:
    throttle(request,'setup',limit=5)
    check_bootstrap(request)
    if await request.app.state.db.fetchone('SELECT username FROM users LIMIT 1'):
        fail(409,'already_initialized','An administrator already exists. Sign in.')
    hashed=await password_hash(request,body.password)
    async with request.app.state.db.transaction() as tx:
        if await tx.fetchone('SELECT username FROM users LIMIT 1'):
            fail(409,'already_initialized','An administrator already exists. Sign in.')
        await tx.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)',(body.username,hashed,'admin'))
        user=await new_session(request,response,{'username':body.username,'role':'admin'},tx)
    await audit.record(request,'auth.setup',body.username)
    return {'user':public_user(user)}


@router.post('/login')
async def login(body: Credentials, request: Request, response: Response) -> dict:
    throttle(request,'login')
    db=request.app.state.db
    user=await db.fetchone('SELECT * FROM users WHERE username=?',(body.username,))
    # A fixed valid hash keeps missing accounts on the same expensive verification path.
    dummy='scrypt:'+'00'*16+':'+'00'*64
    match=await password_matches(request,body.password,user['password_hash'] if user else dummy)
    if not user or not match or user['disabled']:
        await audit.record(request,'auth.login','account',outcome='denied')
        fail(401,'invalid_credentials','Invalid credentials or verification code.')
    async with db.transaction() as tx:
        current=await tx.fetchone('SELECT * FROM users WHERE username=?',(body.username,))
        if not current or current['disabled'] or current['password_hash']!=user['password_hash']:
            fail(401,'invalid_credentials','Account changed. Sign in again.')
        if current['totp_secret_enc'] and not await consume_factor(tx,request.app.state.vault,current,body.code):
            fail(401,'invalid_credentials','Invalid credentials or verification code.')
        session=await new_session(request,response,current,tx)
    await audit.record(request,'auth.login',body.username)
    return {'user':public_user(session)}


@router.post('/logout')
async def logout(request: Request, response: Response) -> dict:
    user=require_role(request,'viewer')
    await request.app.state.db.execute('DELETE FROM auth_sessions WHERE id_hash=?',(user['session_id'],))
    response.delete_cookie(SESSION_COOKIE,path='/',secure=request.app.state.settings.secure_cookies,httponly=True,samesite='strict')
    response.delete_cookie(CSRF_COOKIE,path='/',secure=request.app.state.settings.secure_cookies,samesite='strict')
    await audit.record(request,'auth.logout',user['username'])
    return {'ok':True}


@router.post('/totp/enroll')
async def enroll(body: PasswordCheck, request: Request) -> dict:
    actor=require_role(request,'viewer')
    throttle(request,'totp_enroll')
    db=request.app.state.db
    user=await db.fetchone('SELECT * FROM users WHERE username=?',(actor['username'],))
    if not user or not await password_matches(request,body.password,user['password_hash']):
        fail(401,'invalid_credentials','Verify your password to enroll two-factor authentication.')
    secret=pyotp.random_base32()
    async with db.transaction() as tx:
        await require_current_role(request,'viewer',tx)
        current=await tx.fetchone('SELECT * FROM users WHERE username=?',(actor['username'],))
        if current['password_hash']!=user['password_hash'] or current['disabled']:
            fail(401,'invalid_credentials','Account changed. Sign in again.')
        if current['totp_secret_enc']:
            fail(409,'totp_enabled','Disable existing two-factor authentication before replacing it.')
        await tx.execute('UPDATE users SET totp_pending_enc=? WHERE username=?',(request.app.state.vault.encrypt(secret),actor['username']))
    await audit.record(request,'auth.totp_enrollment_started',actor['username'])
    return {'secret':secret,'uri':pyotp.TOTP(secret).provisioning_uri(name=actor['username'],issuer_name='Inference')}


@router.post('/totp/confirm')
async def confirm(body: Factor, request: Request) -> dict:
    actor=require_role(request,'viewer')
    throttle(request,'totp_confirm')
    async with request.app.state.db.transaction() as tx:
        await require_current_role(request,'viewer',tx)
        user=await tx.fetchone('SELECT * FROM users WHERE username=?',(actor['username'],))
        if not user['totp_pending_enc'] or user['totp_secret_enc']:
            fail(409,'no_enrollment','Start a new two-factor enrollment.')
        secret=request.app.state.vault.decrypt(user['totp_pending_enc'])
        counter=int(time.time())//30
        matched=next((step for step in (counter,counter-1,counter+1) if constant_equal(pyotp.TOTP(secret).at(step*30),body.code)),None)
        if matched is None:
            fail(400,'invalid_code','Verification code is invalid or expired.')
        recovery=[secrets.token_hex(10) for _ in range(10)]
        await tx.execute('UPDATE users SET totp_secret_enc=totp_pending_enc,totp_pending_enc=NULL,totp_last_counter=? WHERE username=?',(matched,actor['username']))
        await tx.execute('DELETE FROM recovery_codes WHERE username=?',(actor['username'],))
        for code in recovery:
            await tx.execute('INSERT INTO recovery_codes(username,code_hash) VALUES(?,?)',(actor['username'],token_hash(code)))
        await tx.execute('DELETE FROM auth_sessions WHERE username=? AND id_hash<>?',(actor['username'],actor['session_id']))
    await audit.record(request,'auth.totp_enabled',actor['username'])
    return {'recovery_codes':recovery}


@router.post('/totp/disable')
async def disable(body: DisableFactor, request: Request) -> dict:
    actor=require_role(request,'viewer')
    throttle(request,'totp_disable')
    db=request.app.state.db
    user=await db.fetchone('SELECT * FROM users WHERE username=?',(actor['username'],))
    if not await password_matches(request,body.password,user['password_hash']):
        fail(401,'invalid_credentials','Invalid credentials or verification code.')
    async with db.transaction() as tx:
        await require_current_role(request,'viewer',tx)
        current=await tx.fetchone('SELECT * FROM users WHERE username=?',(actor['username'],))
        if current['password_hash']!=user['password_hash'] or not await consume_factor(tx,request.app.state.vault,current,body.code):
            fail(401,'invalid_credentials','Invalid credentials or verification code.')
        await tx.execute('UPDATE users SET totp_secret_enc=NULL,totp_pending_enc=NULL,totp_last_counter=-1 WHERE username=?',(actor['username'],))
        await tx.execute('DELETE FROM recovery_codes WHERE username=?',(actor['username'],))
        await tx.execute('DELETE FROM auth_sessions WHERE username=? AND id_hash<>?',(actor['username'],actor['session_id']))
    await audit.record(request,'auth.totp_disabled',actor['username'])
    return {'ok':True}
