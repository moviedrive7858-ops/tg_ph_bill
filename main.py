@dp.message(Command("deduct"))
async def cmd_deduct(message: types.Message):
    if int(message.from_user.id) != OWNER_ID:
        return

    raw_text = message.text.strip()
    args = raw_text.split()
    target_id = None
    target_name = ""
    item_type = "coin"  # default
    amount = 0

    # ၁။ Reply ထောက်၍ သုံးစွဲခြင်း
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if target_user.is_bot:
            await message.reply("❌ Bot များထံမှ နှုတ်ယူ၍ မရပါ။")
            return

        target_id = target_user.id
        target_name = clean_name(target_user.first_name)
        await db.get_or_create_user(target_id, target_user.username, target_user.first_name)

        # /deduct 50 သို့မဟုတ် /deduct coin 50 သို့မဟုတ် /deduct token 1
        if len(args) == 2 and args[1].isdigit():
            amount = int(args[1])
            item_type = "coin"
        elif len(args) >= 3:
            if args[1].lower() in ["token", "tokens"]:
                item_type = "token"
            else:
                item_type = "coin"
            
            for arg in args[1:]:
                if arg.isdigit():
                    amount = int(arg)
                    break
        else:
            await message.reply(
                "💡 <b>Reply ဖြင့် နှုတ်နည်း:</b>\n"
                "• ဘေလ်နှုတ်ရန်: <code>/deduct 50</code> သို့မဟုတ် <code>/deduct coin 50</code>\n"
                "• Token နှုတ်ရန်: <code>/deduct token 1</code>",
                parse_mode="HTML"
            )
            return

    # ၂။ ID သို့မဟုတ် @Username ဖြင့် သုံးစွဲခြင်း (/deduct @username coin 50 သို့မဟုတ် /deduct 123456 coin 50)
    else:
        if len(args) < 3:
            await message.reply(
                "💡 <b>အသုံးပြုနည်း:</b>\n"
                "• Reply ထောက်၍: <code>/deduct coin 50</code>\n"
                "• ID/@username ဖြင့်: <code>/deduct @username coin 50</code>\n"
                "• Token နှုတ်ရန်: <code>/deduct @username token 1</code>",
                parse_mode="HTML"
            )
            return

        target_str = args[1].strip()

        # target user ရှာဖွေခြင်း
        if target_str.isdigit():
            target_id = int(target_str)
            target_name = f"<code>{target_id}</code>"
        elif target_str.startswith("@") or not target_str.isdigit():
            target_id = await db.get_user_by_username(target_str)
            target_name = target_str

        if not target_id:
            await message.reply(f"❌ User <b>{html.escape(target_str)}</b> အား ရှာမတွေ့ပါ သို့မဟုတ် မှတ်တမ်းမရှိသေးပါ။", parse_mode="HTML")
            return

        # token လား coin လား စစ်ဆေးခြင်း
        full_args_str = " ".join(args[2:]).lower()
        if "token" in full_args_str:
            item_type = "token"
        else:
            item_type = "coin"

        for a in args[2:]:
            if a.isdigit():
                amount = int(a)
                break

    if amount <= 0:
        await message.reply("❌ နှုတ်ယူမည့် ပမာဏသည် 0 ထက်ကြီးသော ဂဏန်း ဖြစ်ရပါမည်။")
        return

    # Database မှ နှုတ်ယူခြင်း
    if item_type == "token":
        new_tokens = await db.deduct_tokens(target_id, amount)
        if new_tokens is None:
            new_tokens = 0
        await message.reply(
            f"✅ <b>{target_name}</b> ထံမှ Token <b>{amount} ခု</b> နှုတ်ယူပြီးပါပြီ!\n🎟️ လက်ကျန် Token: <b>{new_tokens} ခု</b>",
            parse_mode="HTML"
        )
    else:
        new_balance = await db.deduct_coins(target_id, amount)
        if new_balance is None:
            new_balance = 0
        await message.reply(
            f"✅ <b>{target_name}</b> ထံမှ ဘေလ် <b>{amount} MMK</b> နှုတ်ယူပြီးပါပြီ!\n💰 လက်ကျန်ငွေ: <b>{new_balance} MMK</b>",
            parse_mode="HTML"
        )
