// SPDX-FileCopyrightText: 2026 CommanderTurtle
// SPDX-License-Identifier: AGPL-3.0-only
const kind=value=>Array.isArray(value)?'array':value!==null&&typeof value==='object'?'object':typeof value;
const schemaKind=hint=>hint?.type==='integer'?'number':hint?.type;
const own=(object,key)=>Object.prototype.hasOwnProperty.call(object,key);
function element(tag,text,classes=''){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(classes)node.className=classes;return node;}
function button(text,action){const node=element('button',text,'small quiet');node.type='button';node.onclick=action;return node;}
export function freshValue(hint={}){
  if(hint.choices?.length)return hint.choices[0];
  if(hint.example!==undefined)return structuredClone(hint.example);
  switch(schemaKind(hint)){
    case 'array':return [];
    case 'object':{const value={};const add=properties=>{for(const [key,field] of Object.entries(properties||{}))if(field.required&&!own(value,key))Object.defineProperty(value,key,{value:freshValue(field),enumerable:true,writable:true,configurable:true});};add(hint.properties);add(hint.variants?.[value[hint.discriminator]]);return value;}
    case 'boolean':return false;
    case 'number':return hint.minimum||0;
    default:return '';
  }
}

export function mountEditor(container,initial,structure,onChange){
  function editable(current,depth=0){if(depth>32)throw new Error('Use at most 32 nested field levels.');if(current===null||typeof current==='number'&&!Number.isFinite(current))throw new Error('TOML values require a concrete type; choose a string, number, boolean, array, or object.');if(typeof current==='object')for(const entry of Object.values(current))editable(entry,depth+1);}
  editable(initial);
  let value=structuredClone(initial),pageLimits=new Map(),openStates=new Map();
  const emit=()=>onChange(structuredClone(value));
  function read(path){let current=value;for(const key of path)current=current[key];return current;}
  function write(path,next){if(!path.length)value=next;else Object.defineProperty(read(path.slice(0,-1)),path.at(-1),{value:next,enumerable:true,writable:true,configurable:true});emit();}
  function hintFor(hint,current){if(hint?.variants){const match=hint.variants[current?.[hint.discriminator]];if(match)return {...hint,type:'object',properties:{...hint.properties,...match}};}return hint||{};}
  function draw(parent,path,hint,label,variantOwner){
    const originalHint=hint;
    const current=read(path);hint=hintFor(hint,current);const type=kind(current);
    const row=element('div',undefined,'structured-node');parent.append(row);
    const header=element('div',undefined,'structured-header');row.append(header);
    const title=element('span',label,'mono');header.append(title);
    const typeInput=element('select');typeInput.setAttribute('aria-label',label+' type');
    for(const t of ['string','number','boolean','array','object']){const option=element('option',t);option.value=t;option.selected=t===type;typeInput.append(option);}
    typeInput.onchange=()=>{write(path,freshValue({...hint,type:typeInput.value,choices:undefined,example:undefined}));render();};header.append(typeInput);
    if(path.length&&!hint.required)header.append(button('Remove',()=>{const owner=read(path.slice(0,-1)),key=path.at(-1);if(Array.isArray(owner))owner.splice(Number(key),1);else delete owner[key];emit();render();}));
    if(hint.help)row.append(element('small',hint.help,'structured-help'));
    if(type==='object'||type==='array'){
      const details=element('details');const identity=JSON.stringify(path);details.open=openStates.get(identity)??path.length<2;const entries=Object.entries(current);details.append(element('summary',entries.length+' '+(type==='array'?'entries':'fields')));row.append(details);
      let mounted=false;
      const mount=()=>{
        if(mounted)return;mounted=true;
        const limit=pageLimits.get(JSON.stringify(path))||40;
        for(const [key] of entries.slice(0,limit)){
          const child=element('div',undefined,'structured-child');details.append(child);
          draw(child,[...path,key],type==='array'?hint.items:hint.properties?.[key]||hint.additional,key,originalHint?.variants&&key===originalHint.discriminator?{path,hint:originalHint}:undefined);
          if(type==='array'){
            const actions=element('div',undefined,'actions');child.append(actions);
            if(Number(key)>0)actions.append(button('Move up',()=>{const index=Number(key);[current[index-1],current[index]]=[current[index],current[index-1]];emit();render();}));
            if(Number(key)<current.length-1)actions.append(button('Move down',()=>{const index=Number(key);[current[index+1],current[index]]=[current[index],current[index+1]];emit();render();}));
          }
        }
        if(entries.length>limit)details.append(button('Show more',()=>{pageLimits.set(JSON.stringify(path),limit+40);render();}));
        if(type==='array')details.append(button('Add entry',()=>{current.push(freshValue(hint.items||{type:'string'}));emit();render();}));
        else{
          const controls=element('div',undefined,'structured-add');details.append(controls);
          const suggestions=Object.keys(hint.properties||{}).filter(key=>!own(current,key));
          const keyInput=element('input');keyInput.placeholder='Field name';keyInput.setAttribute('aria-label',label+' new field');controls.append(keyInput);
          if(suggestions.length){const choose=element('select');choose.setAttribute('aria-label',label+' suggested field');choose.append(element('option','Choose a field…'));for(const key of suggestions){const option=element('option',key);option.value=key;choose.append(option);}choose.onchange=()=>keyInput.value=choose.value;controls.append(choose);}
          controls.append(button('Add field',()=>{const key=keyInput.value.trim();if(!key||own(current,key)){keyInput.setCustomValidity('Choose a new field name.');keyInput.reportValidity();return;}Object.defineProperty(current,key,{value:freshValue(hint.properties?.[key]||hint.additional||{type:'string'}),enumerable:true,writable:true,configurable:true});emit();render();}));
        }
      };
      details.ontoggle=()=>{if(!details.isConnected)return;openStates.set(identity,details.open);if(details.open)mount();};if(details.open)mount();
    }else{
      let input;
      if(hint.choices?.length||type==='boolean'){
        input=element('select');const choices=type==='boolean'?[true,false]:[...new Set([current,...hint.choices])];
        for(const choice of choices){const option=element('option',String(choice));option.value=String(choice);option.selected=current===choice;input.append(option);}
        input.onchange=()=>{
          const next=type==='boolean'?input.value==='true':input.value;
          if(variantOwner){
            const owner=read(variantOwner.path),schema=variantOwner.hint,old=schema.variants[owner[schema.discriminator]]||{},replacement=schema.variants[next]||{};
            Object.defineProperty(owner,schema.discriminator,{value:next,enumerable:true,writable:true,configurable:true});
            for(const key of Object.keys(old))if(!own(replacement,key)&&!own(schema.properties||{},key))delete owner[key];
            for(const [key,field] of Object.entries(replacement))if(field.required&&!own(owner,key))Object.defineProperty(owner,key,{value:freshValue(field),enumerable:true,writable:true,configurable:true});
            emit();render();
          }else{write(path,next);if(hint.choices?.length)render();}
        };
      }else{
        input=element(type==='string'&&String(current).includes('\n')?'textarea':'input');input.value=current??'';
        if(type==='number'){input.type='number';input.step=hint.type==='integer'?'1':'any';if(hint.minimum!==undefined)input.min=hint.minimum;}
        input.oninput=()=>{if(type==='number'){const number=Number(input.value);input.setCustomValidity(input.value===''||!Number.isFinite(number)?'Enter a finite number.':'');if(!input.checkValidity())return;write(path,number);}else write(path,input.value);};
      }
      input.required=Boolean(hint.required);input.setAttribute('aria-label',label+' value');row.append(input);
    }
  }
  function render(){container.replaceChildren();draw(container,[],structure,'Value');}
  render();
  return {get:()=>structuredClone(value),set:next=>{editable(next);value=structuredClone(next);emit();render();}};
}
