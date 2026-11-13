import {request} from './client';
import {Sample, Correction, CorrectionDraft} from '../types/domain';
export const getSamples=(id:number)=>request<Sample[]>(`/studies/${id}/samples`);
export const annotateSample=(id:number,note:string)=>request<Sample>(`/samples/${id}`,{method:'PATCH',body:JSON.stringify({note})});
export const getCorrections=(studyId:number)=>request<Correction[]>(`/studies/${studyId}/corrections`);
export const previewCorrection=(studyId:number, reason:string, items:CorrectionDraft[])=>
  request<Correction>('/corrections/preview',{method:'POST',body:JSON.stringify({study_id:studyId, reason, items})});
export const confirmCorrection=(id:number)=>request<Correction>(`/corrections/${id}/confirm`,{method:'POST'});
